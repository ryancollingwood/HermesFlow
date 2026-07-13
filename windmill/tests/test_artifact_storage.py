"""HF-017 content-addressed artifact storage tests."""
import hashlib
import json
from uuid import uuid4

import pytest

from f.libraries.lineage.models import ArtifactRef, ArtifactStage
from f.libraries.storage.artifacts import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    FilesystemArtifactStore,
)


def write(store, content, **kwargs):
    defaults = {
        "trace_id": uuid4(),
        "stage": ArtifactStage.raw,
        "creator_capability": "f/capabilities/test",
        "creator_capability_version": "1.0.0",
    }
    return store.write(content, **{**defaults, **kwargs})


def test_hashing_and_content_addressed_path(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    ref = write(store, b"hello")
    expected = hashlib.sha256(b"hello").hexdigest()
    assert ref.content_hash == expected
    assert ref.storage_uri == (tmp_path / expected[:2] / expected).as_uri()
    assert store.read(ref) == b"hello"


def test_duplicate_content_reuses_object_but_keeps_lineage_metadata_separate(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    first = write(store, "same", metadata={"source": "one"})
    second = write(store, "same", metadata={"source": "two"})
    assert first.storage_uri == second.storage_uri
    assert first.artifact_id != second.artifact_id
    assert len(list((tmp_path / first.content_hash[:2]).iterdir())) == 1
    assert store.read_metadata(first.artifact_id)["metadata"] == {"source": "one"}
    assert store.read_metadata(second.artifact_id)["metadata"] == {"source": "two"}


def test_text_records_utf8_size_and_media_type(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    ref = write(store, "café")
    assert ref.size_bytes == len("café".encode())
    assert ref.media_type == "text/plain; charset=utf-8"
    assert store.read_text(ref) == "café"


def test_binary_round_trip_and_explicit_media_type(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    content = bytes(range(256))
    ref = write(store, content, media_type="application/pdf")
    assert ref.media_type == "application/pdf"
    assert ref.size_bytes == 256
    assert store.read(ref) == content


def test_derived_artifact_ids_are_retained_in_separate_metadata(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    raw = write(store, "raw")
    derived = write(
        store, "parsed", stage=ArtifactStage.intermediate, derived_from=[raw.artifact_id]
    )
    envelope = store.read_metadata(derived.artifact_id)
    assert envelope["artifact"]["derived_from"] == [str(raw.artifact_id)]


def test_relative_root_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ArtifactStorageError, match="absolute"):
        FilesystemArtifactStore("relative/artifacts")


def test_traversal_uri_is_rejected_before_read(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    digest = hashlib.sha256(b"outside").hexdigest()
    malicious = ArtifactRef(
        trace_id=uuid4(), stage="raw", content_hash=digest,
        storage_uri=(tmp_path / ".." / "outside").as_uri(),
        creator_capability="test", creator_capability_version="1",
    )
    with pytest.raises(ArtifactStorageError, match="escapes configured root"):
        store.read(malicious)


def test_noncanonical_path_inside_root_is_rejected(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    digest = hashlib.sha256(b"x").hexdigest()
    ref = ArtifactRef(
        trace_id=uuid4(), stage="raw", content_hash=digest,
        storage_uri=(tmp_path / "metadata" / "not-content").as_uri(),
        creator_capability="test", creator_capability_version="1",
    )
    with pytest.raises(ArtifactStorageError, match="does not match"):
        store.read(ref)


def test_symlink_escape_is_rejected(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    content = b"outside"
    digest = hashlib.sha256(content).hexdigest()
    outside = tmp_path.parent / f"outside-{uuid4()}"
    outside.write_bytes(content)
    bucket = tmp_path / digest[:2]
    bucket.mkdir()
    (bucket / digest).symlink_to(outside)
    ref = ArtifactRef(
        trace_id=uuid4(), stage="raw", content_hash=digest,
        storage_uri=(bucket / digest).as_uri(), creator_capability="test",
        creator_capability_version="1", size_bytes=len(content), media_type="text/plain",
    )
    try:
        with pytest.raises(ArtifactStorageError, match="escapes configured root"):
            store.read(ref)
    finally:
        outside.unlink()


def test_tampered_content_fails_integrity_check(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    ref = write(store, b"original")
    path = tmp_path / ref.content_hash[:2] / ref.content_hash
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
        store.read(ref)


def test_write_refuses_to_reuse_a_tampered_existing_object(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    content = b"original"
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / digest[:2] / digest
    path.parent.mkdir()
    path.write_bytes(b"wrong")
    with pytest.raises(ArtifactIntegrityError, match="existing content-addressed object"):
        write(store, content)


def test_size_limit_is_enforced_before_write(tmp_path):
    store = FilesystemArtifactStore(tmp_path, max_size_bytes=4)
    with pytest.raises(ArtifactStorageError, match="exceeds limit"):
        write(store, b"12345")
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize(
    "content,media_type",
    [
        ("<html>" + "x" * 1_000_000 + "</html>", "text/html"),
        (json.dumps({"items": list(range(50_000))}), "application/json"),
    ],
)
def test_representative_html_and_json_sizes_round_trip(tmp_path, content, media_type):
    store = FilesystemArtifactStore(tmp_path, max_size_bytes=2_000_000)
    ref = write(store, content, media_type=media_type)
    assert store.read_text(ref) == content


def test_artifacts_persist_when_store_instance_is_recreated(tmp_path):
    ref = write(FilesystemArtifactStore(tmp_path), b"persistent")
    restarted_store = FilesystemArtifactStore(tmp_path)
    assert restarted_store.read(ref) == b"persistent"
    assert restarted_store.read_metadata(ref.artifact_id)["artifact"]["content_hash"] == ref.content_hash
