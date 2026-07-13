"""HF-018 execution-context propagation and lineage-chain tests."""
from uuid import uuid4

import pytest

from f.libraries.lineage.helpers import (
    LineageError,
    LineageState,
    begin_lineage,
    child_context,
    enumerate_artifact_chain,
    record_artifact,
    require_step_context,
    write_artifact,
)
from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext
from f.libraries.storage.artifacts import FilesystemArtifactStore


def boundary(capability="f/workflows/demo"):
    return begin_lineage(
        capability=capability,
        capability_version="1.0.0",
        initiating_actor="u/tester",
        conversation_id="conv-1",
        request_id="req-1",
    )


def test_boundary_generates_missing_context_once():
    state, root = boundary()
    assert state.root_trace_id == root.trace_id
    assert state.contexts == {root.trace_id: root}


def test_boundary_preserves_supplied_context_instead_of_replacing_it():
    supplied = ExecutionContext(
        capability="f/workflows/parent", capability_version="2", initiating_actor="hermes",
        parent_trace_id=uuid4(),
    )
    state, context = begin_lineage(supplied)
    assert context is supplied
    assert context.parent_trace_id == supplied.parent_trace_id
    assert state.root_trace_id == supplied.trace_id


def test_step_refuses_to_generate_missing_context():
    with pytest.raises(LineageError, match="create it at the boundary"):
        require_step_context(None)


def test_child_retains_parent_and_correlation_identifiers():
    state, root = boundary()
    child = child_context(
        state, root, capability="f/capabilities/parse", capability_version="3.1.0"
    )
    assert child.trace_id != root.trace_id
    assert child.parent_trace_id == root.trace_id
    assert child.conversation_id == root.conversation_id
    assert child.request_id == root.request_id
    assert child.initiating_actor == root.initiating_actor


def test_unregistered_parent_from_another_run_is_rejected():
    state, _ = boundary("run-a")
    _, other = boundary("run-b")
    with pytest.raises(LineageError, match="not registered"):
        child_context(state, other, capability="step", capability_version="1")


def test_state_round_trips_for_windmill_step_transport():
    state, root = boundary()
    child_context(state, root, capability="step", capability_version="1")
    restored = LineageState.from_json(state.to_json())
    assert restored == state


def test_raw_to_parsed_to_normalised_to_report_chain(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    state, root = boundary()
    fetch = child_context(state, root, capability="fetch", capability_version="1")
    raw = write_artifact(
        state, store, fetch, "<html>raw</html>", stage=ArtifactStage.raw, media_type="text/html"
    )
    parse = child_context(state, fetch, capability="parse", capability_version="1")
    parsed = write_artifact(
        state, store, parse, '{"value":"raw"}', stage=ArtifactStage.intermediate,
        media_type="application/json", inputs=[raw],
    )
    normalise = child_context(state, parse, capability="normalise", capability_version="1")
    normalised = write_artifact(
        state, store, normalise, '{"value":"RAW"}', stage=ArtifactStage.intermediate,
        media_type="application/json", inputs=[parsed],
    )
    report_ctx = child_context(state, normalise, capability="report", capability_version="1")
    report = write_artifact(
        state, store, report_ctx, "# RAW", stage=ArtifactStage.final,
        media_type="text/markdown", inputs=[normalised],
    )
    chain = enumerate_artifact_chain(state, [report.artifact_id])
    assert [artifact.artifact_id for artifact in chain] == [
        raw.artifact_id, parsed.artifact_id, normalised.artifact_id, report.artifact_id
    ]
    assert [artifact.stage for artifact in chain] == [
        ArtifactStage.raw, ArtifactStage.intermediate, ArtifactStage.intermediate, ArtifactStage.final
    ]


def test_branching_chain_deduplicates_shared_inputs(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    state, root = boundary()
    raw = write_artifact(state, store, root, "raw", stage=ArtifactStage.raw)
    left = write_artifact(
        state, store, root, "left", stage=ArtifactStage.intermediate, inputs=[raw]
    )
    right = write_artifact(
        state, store, root, "right", stage=ArtifactStage.intermediate, inputs=[raw]
    )
    final = write_artifact(
        state, store, root, "final", stage=ArtifactStage.final, inputs=[left, right]
    )
    chain = enumerate_artifact_chain(state, [final.artifact_id])
    assert [item.artifact_id for item in chain].count(raw.artifact_id) == 1


def test_concurrent_runs_cannot_mix_artifacts(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    state_a, context_a = boundary("run-a")
    state_b, context_b = boundary("run-b")
    artifact_a = write_artifact(state_a, store, context_a, "a", stage=ArtifactStage.raw)
    assert state_a.root_trace_id != state_b.root_trace_id
    with pytest.raises(LineageError, match="not registered"):
        write_artifact(
            state_b, store, context_b, "mixed", stage=ArtifactStage.final, inputs=[artifact_a]
        )


def test_record_rejects_artifact_with_wrong_trace(tmp_path):
    state, root = boundary()
    wrong = ArtifactRef(
        trace_id=uuid4(), stage="raw", content_hash="0" * 64,
        storage_uri=(tmp_path / "00" / ("0" * 64)).as_uri(),
        creator_capability="wrong", creator_capability_version="1",
    )
    with pytest.raises(LineageError, match="trace_id"):
        record_artifact(state, root, wrong)


def test_chain_detects_corrupt_cycle():
    state, root = boundary()
    first_id, second_id = uuid4(), uuid4()
    first = ArtifactRef(
        artifact_id=first_id, trace_id=root.trace_id, stage="intermediate",
        content_hash="1" * 64, storage_uri="file:///tmp/one",
        creator_capability="test", creator_capability_version="1", derived_from=[second_id],
    )
    second = ArtifactRef(
        artifact_id=second_id, trace_id=root.trace_id, stage="intermediate",
        content_hash="2" * 64, storage_uri="file:///tmp/two",
        creator_capability="test", creator_capability_version="1", derived_from=[first_id],
    )
    state.artifacts = {first_id: first, second_id: second}
    with pytest.raises(LineageError, match="cycle"):
        enumerate_artifact_chain(state, [first_id])
