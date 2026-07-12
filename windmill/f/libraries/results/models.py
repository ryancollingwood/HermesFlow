"""
Hermes execution result envelope — path: f/libraries/results/models

Other scripts import these directly:

    from f.libraries.results.models import ExecutionResult, ResultOutcome, render_summary

(same import pattern as `f.hermes.client` / `f.libraries.lineage.models` /
`f.libraries.capability.models`.) This is the structure Hermes uses to
present one task's outcome to a user: what happened, what ran and at what
version, the Windmill job that proves it, how long it took, what artifacts
it produced, and — for anything less than a clean success — an actionable
explanation.

Core invariant (architecture/adr/0001-windmill-exclusive-execution.md):
Hermes never claims a Windmill-executed task succeeded without a Windmill
job reference to back it up. `ExecutionResult` enforces this structurally —
see `_job_required_for_claimed_success` below — rather than leaving it to
convention. The one exception ADR 0001 carves out is non-executable
conversation (`execution_type=conversational`): those results never touch
Windmill and so never carry a job reference, by construction.

`render_summary()` is the reference human-presentation renderer; the prose
guidance for how/why it renders each outcome the way it does lives in
docs/result-envelope-rendering.md (destined for
`hermes/skills/workflow-orchestration/hermesflow/references/result-presentation.md`
once HF-005 creates that skill — see the ADR/lifecycle plan for status).

Schema versioning follows the same additive-only-within-a-MAJOR rule as
`f.libraries.lineage.models` — see that module's docstring.

Running THIS script directly exports ExecutionResult's JSON Schema, which
doubles as a self-test — see `docs/schemas/` for the checked-in copy used by
docs/CI (`windmill/tests/test_result_models.py` asserts it matches).
"""
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from f.libraries.lineage.models import ArtifactStage

SCHEMA_VERSION = "1.0"


class ResultOutcome(str, Enum):
    """How a task ended."""

    success = "success"
    partial = "partial"
    failure = "failure"


class ExecutionType(str, Enum):
    """What kind of work this result describes.

    `windmill_job` — task code ran through Windmill (architecture/adr/0001's
    exclusive execution boundary); `conversational` — Hermes answered,
    explained, or drafted text without executing anything (ADR 0001's
    explicit exception).
    """

    windmill_job = "windmill_job"
    conversational = "conversational"


class WindmillJobRef(BaseModel):
    """Points at the Windmill job that proves a windmill_job result actually ran."""

    job_id: str = Field(..., min_length=1)
    workspace: str = Field(default="main", min_length=1)
    path: str = Field(..., min_length=1, description="Script or flow path that ran.")


class ArtifactSummary(BaseModel):
    """A lightweight, presentation-facing pointer to one produced artifact.

    References an ArtifactRef by id rather than duplicating its full record
    (see f.libraries.lineage.models.ArtifactRef) — this is what a result
    envelope shows a user, not the authoritative artifact store entry.
    """

    artifact_id: UUID
    stage: ArtifactStage
    storage_uri: str = Field(..., min_length=1)
    description: Optional[str] = Field(
        default=None, description="Short, human-facing description of what this artifact is."
    )


class CapabilityChangeKind(str, Enum):
    created_candidate = "created_candidate"
    modified_candidate = "modified_candidate"
    promoted = "promoted"


class CapabilityChange(BaseModel):
    """One capability-lifecycle change (architecture/adr/0002) this task caused."""

    path: str = Field(..., min_length=1)
    kind: CapabilityChangeKind
    from_version: Optional[str] = None
    to_version: str = Field(..., min_length=1)


class ExecutionResult(BaseModel):
    """The standard envelope Hermes uses to present one task's outcome."""

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema MAJOR.MINOR this record was written against.",
    )
    outcome: ResultOutcome
    execution_type: ExecutionType
    workflow_path: Optional[str] = Field(
        default=None,
        description="Windmill script/flow path that ran. Required when execution_type "
        "is windmill_job; always None for conversational results.",
    )
    capability_version: Optional[str] = Field(
        default=None,
        description="Version of workflow_path that ran, if known.",
    )
    job: Optional[WindmillJobRef] = Field(
        default=None,
        description="The Windmill job that proves this ran. Required whenever outcome "
        "is success or partial for a windmill_job result. May be absent for a failure "
        "that occurred before a job could be submitted (e.g. Windmill unreachable) or "
        "for a conversational result, where it must always be absent.",
    )
    duration_seconds: Optional[float] = Field(default=None, ge=0)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    capability_changes: list[CapabilityChange] = Field(default_factory=list)
    failure_summary: Optional[str] = Field(
        default=None,
        description="Actionable explanation of what went wrong and, where possible, what "
        "to do about it. Required when outcome is failure.",
    )

    @model_validator(mode="after")
    def _conversational_results_never_carry_a_job(self) -> "ExecutionResult":
        if self.execution_type is ExecutionType.conversational and self.job is not None:
            raise ValueError(
                "a conversational result must not carry a Windmill job reference — "
                "it never touched Windmill (architecture/adr/0001)"
            )
        return self

    @model_validator(mode="after")
    def _job_required_for_claimed_success(self) -> "ExecutionResult":
        if (
            self.execution_type is ExecutionType.windmill_job
            and self.outcome in (ResultOutcome.success, ResultOutcome.partial)
            and self.job is None
        ):
            raise ValueError(
                "a windmill_job result cannot claim success or partial success without "
                "a Windmill job reference (architecture/adr/0001) — this is not "
                "optional even for a 'this should always work' task"
            )
        return self

    @model_validator(mode="after")
    def _failure_requires_a_summary(self) -> "ExecutionResult":
        if self.outcome is ResultOutcome.failure and not (self.failure_summary or "").strip():
            raise ValueError("outcome=failure requires a non-empty failure_summary")
        return self

    @model_validator(mode="after")
    def _windmill_job_requires_a_workflow_path(self) -> "ExecutionResult":
        if self.execution_type is ExecutionType.windmill_job and not (self.workflow_path or "").strip():
            raise ValueError("execution_type=windmill_job requires workflow_path")
        if self.execution_type is ExecutionType.conversational and self.workflow_path is not None:
            raise ValueError("a conversational result must not carry a workflow_path")
        return self


def render_summary(result: ExecutionResult) -> str:
    """Deterministic, human-readable rendering of one result. See
    docs/result-envelope-rendering.md for the presentation rules this follows."""
    lines: list[str] = []

    if result.outcome is ResultOutcome.success:
        lines.append("✓ Succeeded")
    elif result.outcome is ResultOutcome.partial:
        lines.append("⚠ Partially succeeded")
    else:
        lines.append("✗ Failed")

    if result.execution_type is ExecutionType.windmill_job:
        version_suffix = f" (v{result.capability_version})" if result.capability_version else ""
        lines.append(f"  Ran: {result.workflow_path}{version_suffix}")
        if result.job is not None:
            lines.append(f"  Job: {result.job.job_id} (workspace {result.job.workspace})")
        elif result.outcome is ResultOutcome.failure:
            lines.append("  Job: none — failed before a job could be submitted")
    else:
        lines.append("  (conversational — no Windmill execution)")

    if result.duration_seconds is not None:
        lines.append(f"  Duration: {result.duration_seconds:.1f}s")

    if result.artifacts:
        lines.append(f"  Artifacts ({len(result.artifacts)}):")
        for artifact in result.artifacts:
            desc = f" — {artifact.description}" if artifact.description else ""
            lines.append(f"    - [{artifact.stage.value}] {artifact.storage_uri}{desc}")

    if result.capability_changes:
        lines.append(f"  Capability changes ({len(result.capability_changes)}):")
        for change in result.capability_changes:
            from_v = f"{change.from_version} -> " if change.from_version else ""
            lines.append(f"    - {change.kind.value}: {change.path} ({from_v}{change.to_version})")

    if result.warnings:
        lines.append(f"  Warnings ({len(result.warnings)}):")
        for warning in result.warnings:
            lines.append(f"    - {warning}")

    if result.outcome is ResultOutcome.failure:
        lines.append(f"  Why: {result.failure_summary}")

    return "\n".join(lines)


def main() -> dict:
    """Self-test / demo: export ExecutionResult's JSON Schema."""
    return {"ExecutionResult": ExecutionResult.model_json_schema()}
