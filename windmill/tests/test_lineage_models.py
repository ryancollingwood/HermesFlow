"""Unit tests for f/libraries/lineage/models.py — not synced to Windmill (see conftest.py)."""
import hashlib
import json
import pathlib
from uuid import uuid4

import pytest
from f.libraries.lineage.models import (
    ArtifactRef,
    ArtifactStage,
    ArtifactTombstone,
    ExecutionContext,
)
from pydantic import ValidationError

SHA256_OF_EMPTY = hashlib.sha256(b"").hexdigest()
SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"


def make_context(**overrides) -> ExecutionContext:
    defaults = dict(
        capability="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        initiating_actor="hermes",
    )
    defaults.update(overrides)
    return ExecutionContext(**defaults)


def make_artifact(trace_id, **overrides) -> ArtifactRef:
    defaults = dict(
        trace_id=trace_id,
        stage=ArtifactStage.raw,
        content_hash=SHA256_OF_EMPTY,
        storage_uri=f"file:///shared/artifacts/{SHA256_OF_EMPTY[:2]}/{SHA256_OF_EMPTY}",
        creator_capability="f/capabilities/collection/web_fetch",
        creator_capability_version="1.0.0",
    )
    defaults.update(overrides)
    return ArtifactRef(**defaults)


# ── ExecutionContext: valid examples ─────────────────────────────────────────


def test_execution_context_minimal_valid():
    ctx = make_context()
    assert ctx.schema_version == "1.0"
    assert ctx.parent_trace_id is None
    assert ctx.trace_id is not None


def test_execution_context_child_references_parent_trace():
    parent = make_context()
    child = make_context(parent_trace_id=parent.trace_id)
    assert child.parent_trace_id == parent.trace_id
    assert child.trace_id != parent.trace_id


# ── ExecutionContext: invalid examples ───────────────────────────────────────


@pytest.mark.parametrize("missing_field", ["capability", "capability_version", "initiating_actor"])
def test_execution_context_missing_required_field_rejected(missing_field):
    kwargs = dict(
        capability="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        initiating_actor="hermes",
    )
    del kwargs[missing_field]
    with pytest.raises(ValidationError):
        ExecutionContext(**kwargs)


def test_execution_context_cannot_be_its_own_parent():
    trace_id = uuid4()
    with pytest.raises(ValidationError):
        ExecutionContext(
            trace_id=trace_id,
            parent_trace_id=trace_id,
            capability="c",
            capability_version="1",
            initiating_actor="x",
        )


def test_execution_context_empty_capability_rejected():
    with pytest.raises(ValidationError):
        make_context(capability="")


# ── ArtifactRef: valid examples ──────────────────────────────────────────────


def test_artifact_ref_raw_intermediate_final_stages():
    ctx = make_context()
    for stage in (ArtifactStage.raw, ArtifactStage.intermediate, ArtifactStage.final):
        artifact = make_artifact(ctx.trace_id, stage=stage)
        assert artifact.stage is stage


def test_artifact_ref_content_hash_normalized_to_lowercase():
    ctx = make_context()
    artifact = make_artifact(ctx.trace_id, content_hash=SHA256_OF_EMPTY.upper())
    assert artifact.content_hash == SHA256_OF_EMPTY  # lowercased


# ── ArtifactRef: invalid examples ────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_hash",
    ["too-short", "z" * 64, SHA256_OF_EMPTY[:-1], ""],
)
def test_artifact_ref_rejects_non_sha256_hash(bad_hash):
    ctx = make_context()
    with pytest.raises(ValidationError):
        make_artifact(ctx.trace_id, content_hash=bad_hash)


def test_artifact_ref_requires_trace_id():
    with pytest.raises(ValidationError):
        ArtifactRef(
            stage=ArtifactStage.raw,
            content_hash=SHA256_OF_EMPTY,
            storage_uri="file:///x",
            creator_capability="c",
            creator_capability_version="1",
        )


def test_artifact_ref_cannot_derive_from_itself():
    ctx = make_context()
    self_id = uuid4()
    with pytest.raises(ValidationError):
        make_artifact(ctx.trace_id, artifact_id=self_id, derived_from=[self_id])


# ── Schema versioning / backward compatibility ──────────────────────────────


def test_schema_version_defaults_and_is_overridable():
    ctx = make_context()
    assert ctx.schema_version == "1.0"
    older = ExecutionContext(
        schema_version="1.0",
        capability="c",
        capability_version="1",
        initiating_actor="x",
    )
    assert older.schema_version == "1.0"


def test_older_payload_without_newer_optional_fields_still_validates():
    # Simulates a payload written before conversation_id/request_id existed:
    # both are optional, so a record lacking them must still validate under
    # the current model (the additive-optional-fields compatibility rule).
    old_shaped_payload = {
        "schema_version": "1.0",
        "capability": "f/capabilities/collection/web_fetch",
        "capability_version": "1.0.0",
        "initiating_actor": "hermes",
    }
    ctx = ExecutionContext(**old_shaped_payload)
    assert ctx.conversation_id is None
    assert ctx.request_id is None


def test_unknown_extra_field_is_ignored_not_rejected():
    # Forward compatibility: a payload from a newer MINOR version carrying a
    # field this model doesn't know about must not fail validation.
    payload = {
        "capability": "c",
        "capability_version": "1",
        "initiating_actor": "x",
        "some_future_field": "unused-by-this-version",
    }
    ctx = ExecutionContext(**payload)
    assert not hasattr(ctx, "some_future_field")


# ── Lineage: raw -> transformation -> final report ──────────────────────────


def test_lineage_chain_raw_to_transformation_to_final():
    ctx = make_context(capability="f/workflows/product_collection")

    raw = make_artifact(
        ctx.trace_id,
        stage=ArtifactStage.raw,
        content_hash=hashlib.sha256(b"raw-page.html").hexdigest(),
    )

    transformed_hash = hashlib.sha256(b"normalized-product.json").hexdigest()
    transformation = make_artifact(
        ctx.trace_id,
        stage=ArtifactStage.intermediate,
        content_hash=transformed_hash,
        storage_uri=f"file:///shared/artifacts/{transformed_hash[:2]}/{transformed_hash}",
        derived_from=[raw.artifact_id],
    )

    report_hash = hashlib.sha256(b"comparison-report.md").hexdigest()
    final_report = make_artifact(
        ctx.trace_id,
        stage=ArtifactStage.final,
        content_hash=report_hash,
        storage_uri=f"file:///shared/artifacts/{report_hash[:2]}/{report_hash}",
        derived_from=[transformation.artifact_id],
    )

    # Every artifact in the chain traces back to the same execution.
    assert raw.trace_id == transformation.trace_id == final_report.trace_id == ctx.trace_id

    # Stages are exactly the raw -> intermediate -> final progression.
    assert [raw.stage, transformation.stage, final_report.stage] == [
        ArtifactStage.raw,
        ArtifactStage.intermediate,
        ArtifactStage.final,
    ]

    # Derivation links form an unbroken chain back to the raw artifact.
    assert transformation.derived_from == [raw.artifact_id]
    assert final_report.derived_from == [transformation.artifact_id]
    assert raw.derived_from == []


# ── ArtifactTombstone: HF-035 lineage-preserving deletion record ────────────


def test_tombstone_carries_same_lineage_fields_as_the_deleted_ref():
    ctx = make_context()
    ref = make_artifact(ctx.trace_id, derived_from=[uuid4()])
    tombstone = ArtifactTombstone(
        artifact_id=ref.artifact_id,
        trace_id=ref.trace_id,
        stage=ref.stage,
        content_hash=ref.content_hash,
        creator_capability=ref.creator_capability,
        creator_capability_version=ref.creator_capability_version,
        derived_from=ref.derived_from,
        reason="retention expiry",
    )
    assert tombstone.artifact_id == ref.artifact_id
    assert tombstone.derived_from == ref.derived_from
    assert tombstone.reason == "retention expiry"


def test_tombstone_requires_a_non_empty_reason():
    ctx = make_context()
    ref = make_artifact(ctx.trace_id)
    with pytest.raises(ValidationError):
        ArtifactTombstone(
            artifact_id=ref.artifact_id, trace_id=ref.trace_id, stage=ref.stage,
            content_hash=ref.content_hash, creator_capability=ref.creator_capability,
            creator_capability_version=ref.creator_capability_version, reason="",
        )


# ── docs/CI: checked-in JSON Schema exports must match the models ───────────


@pytest.mark.parametrize(
    "model,filename",
    [
        (ExecutionContext, "execution_context.schema.json"),
        (ArtifactRef, "artifact_ref.schema.json"),
        (ArtifactTombstone, "artifact_tombstone.schema.json"),
    ],
)
def test_checked_in_json_schema_matches_model(model, filename):
    schema_path = SCHEMAS_DIR / filename
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        f"python -c \"import json; from f.libraries.lineage.models import {model.__name__}; "
        f"print(json.dumps({model.__name__}.model_json_schema(), indent=2, sort_keys=True))\" "
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(model.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to {model.__name__} — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
