"""Unit tests for f/libraries/results/models.py — not synced to Windmill (see conftest.py)."""
import json
import pathlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from f.libraries.lineage.models import ArtifactStage
from f.libraries.results.models import (
    ArtifactSummary,
    CapabilityChange,
    CapabilityChangeKind,
    ExecutionResult,
    ExecutionType,
    ResultOutcome,
    WindmillJobRef,
    render_summary,
)

SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"
FIXED_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000001")


def make_job(**overrides) -> WindmillJobRef:
    defaults = dict(job_id="019f5700-862a-87e4-8b53-e747845a01f8", path="f/capabilities/collection/web_fetch")
    defaults.update(overrides)
    return WindmillJobRef(**defaults)


def make_success(**overrides) -> ExecutionResult:
    defaults = dict(
        outcome=ResultOutcome.success,
        execution_type=ExecutionType.windmill_job,
        workflow_path="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        job=make_job(),
        duration_seconds=2.345,
        artifacts=[
            ArtifactSummary(
                artifact_id=FIXED_ARTIFACT_ID,
                stage=ArtifactStage.final,
                storage_uri="file:///shared/artifacts/ab/abc123",
                description="fetched page",
            )
        ],
    )
    defaults.update(overrides)
    return ExecutionResult(**defaults)


# ── Machine-readable model: valid examples ───────────────────────────────────


def test_conversational_result_needs_no_job_or_workflow_path():
    result = ExecutionResult(outcome=ResultOutcome.success, execution_type=ExecutionType.conversational)
    assert result.job is None
    assert result.workflow_path is None


def test_windmill_job_success_includes_code_version_and_artifacts():
    result = make_success()
    assert result.workflow_path == "f/capabilities/collection/web_fetch"
    assert result.capability_version == "1.0.0"
    assert result.job.job_id
    assert len(result.artifacts) == 1


def test_capability_changes_are_recorded():
    change = CapabilityChange(
        path="f/hermes_flow/candidates/new_report_step",
        kind=CapabilityChangeKind.created_candidate,
        to_version="0.1.0",
    )
    result = make_success(capability_changes=[change])
    assert result.capability_changes[0].kind is CapabilityChangeKind.created_candidate


# ── Core invariant: no claimed success without a job reference ──────────────


def test_windmill_job_success_without_job_reference_is_rejected():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.success,
            execution_type=ExecutionType.windmill_job,
            workflow_path="f/capabilities/collection/web_fetch",
        )


def test_windmill_job_partial_without_job_reference_is_rejected():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.partial,
            execution_type=ExecutionType.windmill_job,
            workflow_path="f/capabilities/collection/web_fetch",
            failure_summary="one of three sources failed",
        )


def test_windmill_job_failure_may_omit_job_reference():
    # Covers ADR 0001's Windmill-unreachable failure mode: the task never
    # got far enough to be submitted, so there is no job to reference.
    result = ExecutionResult(
        outcome=ResultOutcome.failure,
        execution_type=ExecutionType.windmill_job,
        workflow_path="f/capabilities/collection/web_fetch",
        failure_summary="Windmill was unreachable — task not submitted. Retry once the server is back.",
    )
    assert result.job is None


def test_conversational_result_cannot_carry_a_job_reference():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.success,
            execution_type=ExecutionType.conversational,
            job=make_job(),
        )


def test_conversational_result_cannot_carry_a_workflow_path():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.success,
            execution_type=ExecutionType.conversational,
            workflow_path="f/capabilities/collection/web_fetch",
        )


def test_windmill_job_requires_a_workflow_path():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.failure,
            execution_type=ExecutionType.windmill_job,
            failure_summary="never even resolved a path to run",
        )


# ── Failed runs include an actionable failure summary ────────────────────────


def test_failure_without_summary_is_rejected():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.failure,
            execution_type=ExecutionType.windmill_job,
            workflow_path="f/capabilities/collection/web_fetch",
            job=make_job(),
        )


def test_failure_with_blank_summary_is_rejected():
    with pytest.raises(ValidationError):
        ExecutionResult(
            outcome=ResultOutcome.failure,
            execution_type=ExecutionType.windmill_job,
            workflow_path="f/capabilities/collection/web_fetch",
            job=make_job(),
            failure_summary="   ",
        )


def test_failed_run_with_job_reference_and_summary_is_valid():
    result = ExecutionResult(
        outcome=ResultOutcome.failure,
        execution_type=ExecutionType.windmill_job,
        workflow_path="f/capabilities/collection/web_fetch",
        job=make_job(),
        failure_summary="TypeError: 'NoneType' object is not subscriptable — the conn argument "
        "was not resolved. Re-run with a valid f/hermes/local resource.",
    )
    assert result.job is not None
    assert "conn argument" in result.failure_summary


# ── Snapshot tests: success, failure, partial rendering ──────────────────────


def test_render_success_snapshot():
    result = make_success()
    assert render_summary(result) == (
        "✓ Succeeded\n"
        "  Ran: f/capabilities/collection/web_fetch (v1.0.0)\n"
        "  Job: 019f5700-862a-87e4-8b53-e747845a01f8 (workspace main)\n"
        "  Duration: 2.3s\n"
        "  Artifacts (1):\n"
        "    - [final] file:///shared/artifacts/ab/abc123 — fetched page"
    )


def test_render_failure_snapshot():
    result = ExecutionResult(
        outcome=ResultOutcome.failure,
        execution_type=ExecutionType.windmill_job,
        workflow_path="f/hermes/client",
        job=make_job(job_id="019f55ef-e96c-a968-3e94-5610d732b37b", path="f/hermes/client"),
        duration_seconds=0.8,
        failure_summary="TypeError: 'NoneType' object is not subscriptable — conn was None.",
    )
    assert render_summary(result) == (
        "✗ Failed\n"
        "  Ran: f/hermes/client\n"
        "  Job: 019f55ef-e96c-a968-3e94-5610d732b37b (workspace main)\n"
        "  Duration: 0.8s\n"
        "  Why: TypeError: 'NoneType' object is not subscriptable — conn was None."
    )


def test_render_partial_snapshot():
    result = ExecutionResult(
        outcome=ResultOutcome.partial,
        execution_type=ExecutionType.windmill_job,
        workflow_path="f/workflows/product_collection",
        job=make_job(job_id="019f5701-0000-0000-0000-000000000000", path="f/workflows/product_collection"),
        duration_seconds=41.2,
        warnings=["source 'retailer-b' returned 0 products — page layout may have changed"],
        artifacts=[
            ArtifactSummary(
                artifact_id=FIXED_ARTIFACT_ID,
                stage=ArtifactStage.final,
                storage_uri="file:///shared/artifacts/cd/cdef01",
                description="comparison report (2 of 3 sources)",
            )
        ],
    )
    assert render_summary(result) == (
        "⚠ Partially succeeded\n"
        "  Ran: f/workflows/product_collection\n"
        "  Job: 019f5701-0000-0000-0000-000000000000 (workspace main)\n"
        "  Duration: 41.2s\n"
        "  Artifacts (1):\n"
        "    - [final] file:///shared/artifacts/cd/cdef01 — comparison report (2 of 3 sources)\n"
        "  Warnings (1):\n"
        "    - source 'retailer-b' returned 0 products — page layout may have changed"
    )


def test_render_conversational_snapshot():
    result = ExecutionResult(outcome=ResultOutcome.success, execution_type=ExecutionType.conversational)
    assert render_summary(result) == "✓ Succeeded\n  (conversational — no Windmill execution)"


# ── docs/CI: checked-in JSON Schema export must match the model ─────────────


def test_checked_in_json_schema_matches_model():
    schema_path = SCHEMAS_DIR / "execution_result.schema.json"
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        "python -c \"import json; from f.libraries.results.models import ExecutionResult; "
        'print(json.dumps(ExecutionResult.model_json_schema(), indent=2, sort_keys=True))" '
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(ExecutionResult.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to ExecutionResult — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
