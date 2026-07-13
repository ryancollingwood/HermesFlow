"""HF-022 deterministic JSON-LD extraction from retained web artifacts."""
from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from f.libraries.lineage.helpers import LineageState
from f.libraries.lineage.models import ArtifactRef
from f.libraries.storage.artifacts import FilesystemArtifactStore

CAPABILITY_PATH = "f/capabilities/collection/extract_structured_markup"
CAPABILITY_VERSION = "1.0.0"


class ExtractionProvenance(BaseModel):
    parser: str = "json-ld"
    parser_version: str = CAPABILITY_VERSION
    source_artifact_id: str
    source_content_hash: str
    source_url: Optional[str] = None
    block_index: int = Field(..., ge=0)
    source_path: str


class StructuredCandidate(BaseModel):
    candidate_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    types: list[str] = Field(default_factory=list)
    identifier: Optional[str] = None
    name: Optional[str] = None
    url: Optional[str] = None
    data: dict[str, Any]
    provenance: ExtractionProvenance

    @field_validator("types")
    @classmethod
    def _unique_types(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in value if item))


class ExtractionWarning(BaseModel):
    block_index: Optional[int] = None
    code: str
    message: str


class StructuredMarkupResult(BaseModel):
    schema_version: str = "1.0"
    status: str
    parser_version: str = CAPABILITY_VERSION
    source_artifact: ArtifactRef
    blocks_found: int
    blocks_parsed: int
    candidates: list[StructuredCandidate]
    warnings: list[ExtractionWarning] = Field(default_factory=list)


class _JsonLdParser(HTMLParser):
    def __init__(self, max_blocks: int):
        super().__init__(convert_charrefs=False)
        self.max_blocks = max_blocks
        self.blocks: list[str] = []
        self.blocks_found = 0
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "script" or self._capturing:
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        media_type = attributes.get("type", "").split(";", 1)[0].strip().lower()
        if media_type == "application/ld+json":
            self.blocks_found += 1
            self._capturing = self.blocks_found <= self.max_blocks
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.blocks.append("".join(self._parts))
            self._capturing = False
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)


def _types(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _optional_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _candidate_id(data: dict[str, Any], provenance: ExtractionProvenance) -> str:
    payload = {
        "data": data,
        "source_content_hash": provenance.source_content_hash,
        "block_index": provenance.block_index,
        "source_path": provenance.source_path,
        "parser_version": provenance.parser_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _nodes(value: Any) -> tuple[list[tuple[dict[str, Any], str]], Optional[str]]:
    if isinstance(value, dict):
        graph = value.get("@graph")
        if graph is None:
            return [(value, "$")], None
        if not isinstance(graph, list):
            return [], "@graph must be an array"
        nodes = [
            (item, f"$.@graph[{index}]")
            for index, item in enumerate(graph)
            if isinstance(item, dict)
        ]
        ignored = len(graph) - len(nodes)
        return nodes, f"ignored {ignored} non-object @graph item(s)" if ignored else None
    if isinstance(value, list):
        nodes = [
            (item, f"$[{index}]")
            for index, item in enumerate(value)
            if isinstance(item, dict)
        ]
        ignored = len(value) - len(nodes)
        return nodes, f"ignored {ignored} non-object array item(s)" if ignored else None
    return [], "JSON-LD root must be an object or array of objects"


def _artifact_source_url(store: FilesystemArtifactStore, artifact: ArtifactRef) -> Optional[str]:
    metadata = store.read_metadata(artifact.artifact_id).get("metadata", {})
    return _optional_string(metadata.get("url"))


def extract_structured_markup(
    raw_artifact: ArtifactRef,
    *,
    lineage: Optional[LineageState] = None,
    store: Optional[FilesystemArtifactStore] = None,
    max_html_bytes: int = 10_000_000,
    max_json_bytes: int = 1_000_000,
    max_blocks: int = 100,
    max_candidates: int = 1000,
) -> StructuredMarkupResult:
    if max_html_bytes <= 0 or max_html_bytes > 100_000_000:
        raise ValueError("max_html_bytes must be between 1 and 100000000")
    if max_json_bytes <= 0 or max_json_bytes > 10_000_000:
        raise ValueError("max_json_bytes must be between 1 and 10000000")
    if max_blocks <= 0 or max_blocks > 1000:
        raise ValueError("max_blocks must be between 1 and 1000")
    if max_candidates <= 0 or max_candidates > 10_000:
        raise ValueError("max_candidates must be between 1 and 10000")
    if lineage is not None and lineage.artifacts.get(raw_artifact.artifact_id) != raw_artifact:
        raise ValueError("raw artifact is not registered in the supplied lineage state")
    artifact_store = store or FilesystemArtifactStore()
    raw = artifact_store.read(raw_artifact)
    if len(raw) > max_html_bytes:
        raise ValueError(f"source artifact exceeds {max_html_bytes} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return StructuredMarkupResult(
            status="invalid_encoding",
            source_artifact=raw_artifact,
            blocks_found=0,
            blocks_parsed=0,
            candidates=[],
            warnings=[ExtractionWarning(
                code="invalid_encoding", message="source artifact is not valid UTF-8"
            )],
        )

    media_type = (raw_artifact.media_type or "").split(";", 1)[0].strip().lower()
    if media_type == "application/ld+json":
        blocks = [text]
        blocks_found = 1
    else:
        parser = _JsonLdParser(max_blocks=max_blocks)
        parser.feed(text)
        parser.close()
        blocks = parser.blocks
        blocks_found = parser.blocks_found

    warnings: list[ExtractionWarning] = []
    if blocks_found > max_blocks:
        warnings.append(ExtractionWarning(
            code="block_limit",
            message=f"found {blocks_found} blocks; parsed only the first {max_blocks}",
        ))
    candidates: list[StructuredCandidate] = []
    blocks_parsed = 0
    candidate_limit_hit = False
    source_url = _artifact_source_url(artifact_store, raw_artifact)
    for block_index, block in enumerate(blocks):
        if len(block.encode()) > max_json_bytes:
            warnings.append(ExtractionWarning(
                block_index=block_index,
                code="block_too_large",
                message=f"JSON-LD block exceeds {max_json_bytes} bytes",
            ))
            continue
        try:
            value = json.loads(block)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            warnings.append(ExtractionWarning(
                block_index=block_index,
                code="malformed_json_ld",
                message=f"JSON-LD parse error: {exc.msg if hasattr(exc, 'msg') else exc}",
            ))
            continue
        blocks_parsed += 1
        nodes, node_warning = _nodes(value)
        if node_warning:
            warnings.append(ExtractionWarning(
                block_index=block_index, code="invalid_json_ld_shape", message=node_warning
            ))
        for data, source_path in nodes:
            if not data or set(data) <= {"@context"}:
                warnings.append(ExtractionWarning(
                    block_index=block_index,
                    code="context_only_node",
                    message=f"ignored non-candidate node at {source_path}",
                ))
                continue
            if len(candidates) >= max_candidates:
                warnings.append(ExtractionWarning(
                    code="candidate_limit",
                    message=f"candidate count limited to {max_candidates}",
                ))
                candidate_limit_hit = True
                break
            provenance = ExtractionProvenance(
                source_artifact_id=str(raw_artifact.artifact_id),
                source_content_hash=raw_artifact.content_hash,
                source_url=source_url,
                block_index=block_index,
                source_path=source_path,
            )
            candidates.append(StructuredCandidate(
                candidate_id=_candidate_id(data, provenance),
                types=_types(data.get("@type")),
                identifier=_optional_string(data.get("@id")),
                name=_optional_string(data.get("name")),
                url=_optional_string(data.get("url")),
                data=data,
                provenance=provenance,
            ))
        if candidate_limit_hit:
            break

    if candidates:
        status = "success"
    elif blocks_found == 0:
        status = "no_markup"
    else:
        status = "no_valid_candidates"
    return StructuredMarkupResult(
        status=status,
        source_artifact=raw_artifact,
        blocks_found=blocks_found,
        blocks_parsed=blocks_parsed,
        candidates=candidates,
        warnings=warnings,
    )


def main(
    raw_artifact: dict,
    lineage_json: str = "",
    max_html_bytes: int = 10_000_000,
    max_json_bytes: int = 1_000_000,
    max_blocks: int = 100,
    max_candidates: int = 1000,
) -> dict:
    result = extract_structured_markup(
        ArtifactRef.model_validate(raw_artifact),
        lineage=LineageState.from_json(lineage_json) if lineage_json else None,
        max_html_bytes=max_html_bytes,
        max_json_bytes=max_json_bytes,
        max_blocks=max_blocks,
        max_candidates=max_candidates,
    )
    return result.model_dump(mode="json")
