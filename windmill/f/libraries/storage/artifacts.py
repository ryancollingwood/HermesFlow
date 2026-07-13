"""HF-017 content-addressed filesystem artifact storage adapter."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse
from uuid import UUID

from f.libraries.lineage.models import ArtifactRef, ArtifactStage

DEFAULT_ARTIFACT_ROOT = Path("/shared/artifacts")


class ArtifactStorageError(ValueError):
    pass


class ArtifactIntegrityError(ArtifactStorageError):
    pass


class FilesystemArtifactStore:
    def __init__(self, root: Path | str = DEFAULT_ARTIFACT_ROOT, max_size_bytes: int = 100_000_000):
        root_path = Path(root).expanduser()
        if not root_path.is_absolute():
            raise ArtifactStorageError("artifact root must be an absolute path")
        if max_size_bytes <= 0:
            raise ArtifactStorageError("max_size_bytes must be positive")
        self.root = root_path.resolve()
        self.max_size_bytes = max_size_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "metadata").mkdir(exist_ok=True)

    @staticmethod
    def hash_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _content_path(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ArtifactStorageError("invalid SHA-256 content hash")
        return self.root / content_hash[:2] / content_hash

    def _ensure_contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ArtifactStorageError("artifact path escapes configured root")
        return resolved

    @staticmethod
    def _atomic_write(path: Path, content: bytes, exclusive: bool = False) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if exclusive:
                raise ArtifactStorageError(f"metadata already exists at {path.name}")
            return False
        fd, tmp_name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive and path.exists():
                raise ArtifactStorageError(f"metadata already exists at {path.name}")
            os.replace(tmp_name, path)
            return True
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def write(
        self,
        content: bytes | str,
        *,
        trace_id: UUID,
        stage: ArtifactStage,
        creator_capability: str,
        creator_capability_version: str,
        media_type: Optional[str] = None,
        derived_from: Optional[list[UUID]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ArtifactRef:
        if isinstance(content, str):
            raw = content.encode("utf-8")
            resolved_media_type = media_type or "text/plain; charset=utf-8"
        elif isinstance(content, bytes):
            raw = content
            resolved_media_type = media_type or "application/octet-stream"
        else:
            raise ArtifactStorageError("content must be bytes or str")
        if len(raw) > self.max_size_bytes:
            raise ArtifactStorageError(
                f"artifact size {len(raw)} exceeds limit {self.max_size_bytes}"
            )
        content_hash = self.hash_bytes(raw)
        path = self._ensure_contained(self._content_path(content_hash))
        created = self._atomic_write(path, raw)
        if not created:
            existing = path.read_bytes()
            if self.hash_bytes(existing) != content_hash or existing != raw:
                raise ArtifactIntegrityError(
                    "existing content-addressed object does not match the bytes being stored"
                )
        ref = ArtifactRef(
            trace_id=trace_id,
            stage=stage,
            content_hash=content_hash,
            storage_uri=path.as_uri(),
            size_bytes=len(raw),
            media_type=resolved_media_type,
            creator_capability=creator_capability,
            creator_capability_version=creator_capability_version,
            derived_from=derived_from or [],
        )
        metadata_path = self._ensure_contained(self.root / "metadata" / f"{ref.artifact_id}.json")
        envelope = {
            "artifact": ref.model_dump(mode="json"),
            "metadata": metadata or {},
        }
        self._atomic_write(
            metadata_path,
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            exclusive=True,
        )
        return ref

    def _path_from_ref(self, ref: ArtifactRef) -> Path:
        parsed = urlparse(ref.storage_uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise ArtifactStorageError("only local file:// artifact URIs are supported")
        supplied = Path(unquote(parsed.path))
        canonical = self._ensure_contained(self._content_path(ref.content_hash))
        resolved = self._ensure_contained(supplied)
        if resolved != canonical:
            raise ArtifactStorageError("storage_uri does not match the content-addressed path")
        return resolved

    def read(self, ref: ArtifactRef) -> bytes:
        path = self._path_from_ref(ref)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactStorageError(f"artifact object is missing: {ref.content_hash}") from exc
        if self.hash_bytes(content) != ref.content_hash:
            raise ArtifactIntegrityError("stored artifact content does not match its SHA-256 reference")
        if ref.size_bytes is not None and len(content) != ref.size_bytes:
            raise ArtifactIntegrityError("stored artifact size does not match its reference")
        return content

    def read_text(self, ref: ArtifactRef, encoding: str = "utf-8") -> str:
        return self.read(ref).decode(encoding)

    def read_metadata(self, artifact_id: UUID) -> dict[str, Any]:
        path = self._ensure_contained(self.root / "metadata" / f"{artifact_id}.json")
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise ArtifactStorageError(f"artifact metadata is missing: {artifact_id}") from exc


def main() -> dict:
    """Windmill smoke check without writing: report the configured root."""
    store = FilesystemArtifactStore()
    return {"root": str(store.root), "max_size_bytes": store.max_size_bytes}
