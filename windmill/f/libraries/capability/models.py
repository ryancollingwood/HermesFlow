"""
Capability metadata and autonomy schema — path: f/libraries/capability/models

Other scripts import these directly:

    from f.libraries.capability.models import CapabilityMetadata, AutonomyPolicy

(same import pattern as `f.hermes.client` / `f.libraries.lineage.models`.)
Every discoverable capability (a Windmill script/flow HermesFlow can select,
compose, or run) is described by one `CapabilityMetadata` record. This will
feed `windmill/capability-index.yaml` (HF-008) once the catalogue loader
exists; for now this module is the schema + validation rules that record is
built against.

Deliberately NOT modelled here: a capability's own argument schema, resource
types, or anything else Windmill's `*.script.yaml` is already authoritative
for. `CapabilityMetadata.path` points at that script; agents read the
script's own schema from Windmill, not a duplicate copy here. This module
only carries the agent-selection metadata Windmill has no concept of:
discovery text, maturity, effects, autonomy policy, limits, test
requirements, dependencies, ownership.

Schema versioning follows the same additive-only-within-a-MAJOR rule as
`f.libraries.lineage.models` — see that module's docstring.

Running THIS script directly exports both models' JSON Schemas, which
doubles as a self-test — see `docs/schemas/` for the checked-in copies used
by docs/CI (`windmill/tests/test_capability_models.py` asserts they match).
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"


class CapabilityMaturity(str, Enum):
    """How much this capability has proven itself in production."""

    experimental = "experimental"
    stable = "stable"
    deprecated = "deprecated"


class AutonomyLevel(str, Enum):
    """Whether an action may run unattended or needs a human approval step."""

    automatic = "automatic"
    approval_required = "approval_required"


class AutonomyAction(str, Enum):
    """The lifecycle actions a capability's autonomy policy governs.

    Matches the lifecycle in architecture/adr/0002-capability-lifecycle.md:
    REQUESTED -> SEARCH (discover) -> COMPOSE/REUSE/GENERATE (compose) ->
    CANDIDATE (create_candidate) -> TESTED -> ACTIVE -> EXECUTE (execute) ->
    ... -> PATCH (modify_candidate) -> TEST -> PROMOTE (promote). `schedule`
    covers turning a capability into a recurring Windmill schedule.
    """

    discover = "discover"
    execute = "execute"
    compose = "compose"
    create_candidate = "create_candidate"
    modify_candidate = "modify_candidate"
    promote = "promote"
    schedule = "schedule"


# Per architecture/adr/0001 ("no active-code mutation" is not Hermes's to own)
# and architecture/adr/0002 ("promotion to active, schedule changes ... require
# policy-gated approval"), these two actions are never automatic for ANY
# capability. This is a platform-wide invariant, not a per-capability choice —
# no metadata field, including a low-risk maturity/effects label, can imply
# otherwise. Enforced structurally below, not just by convention.
ALWAYS_APPROVAL_REQUIRED: tuple[AutonomyAction, ...] = (
    AutonomyAction.promote,
    AutonomyAction.schedule,
)


class AutonomyPolicy(BaseModel):
    """Per-action autonomy for one capability. See ALWAYS_APPROVAL_REQUIRED."""

    discover: AutonomyLevel = AutonomyLevel.automatic
    execute: AutonomyLevel = AutonomyLevel.automatic
    compose: AutonomyLevel = AutonomyLevel.automatic
    create_candidate: AutonomyLevel = AutonomyLevel.automatic
    modify_candidate: AutonomyLevel = AutonomyLevel.automatic
    promote: AutonomyLevel = AutonomyLevel.approval_required
    schedule: AutonomyLevel = AutonomyLevel.approval_required

    @field_validator("promote", "schedule")
    @classmethod
    def _promote_and_schedule_always_require_approval(cls, v: AutonomyLevel) -> AutonomyLevel:
        if v is not AutonomyLevel.approval_required:
            raise ValueError(
                "promote and schedule must always be approval_required — "
                "see architecture/adr/0002-capability-lifecycle.md. No "
                "capability metadata may grant automatic promotion or "
                "scheduling, regardless of maturity or effects."
            )
        return v

    def level_for(self, action: AutonomyAction) -> AutonomyLevel:
        return getattr(self, action.value)


class CapabilityEffects(BaseModel):
    """Side-effect categories this capability may cause when it runs."""

    network: bool = False
    filesystem: bool = False
    database: bool = False
    external: bool = Field(
        default=False,
        description="Any other externally-visible side effect not covered above "
        "(sending a notification, calling a third-party API that isn't a plain "
        "network fetch, etc.).",
    )

    @property
    def is_side_effect_free(self) -> bool:
        return not (self.network or self.filesystem or self.database or self.external)


class CapabilityLimits(BaseModel):
    """Bounds enforced when this capability runs. All optional — unset means unbounded.

    HF-035 adds the size/record-count/cost triad alongside the pre-existing
    ``timeout_seconds`` (duration): together these are the four retention/cost
    dimensions `f.hermes_flow.policies.evaluator.evaluate_policy` can deny a
    request for, the same way it already denies on `max_concurrency`/
    `rate_limit_per_minute`. Declaring a limit here does not itself measure or
    estimate anything — evaluation only compares whatever the caller reports it
    expects (``PolicyContext.requested_*``) against the declared bound.
    """

    timeout_seconds: Optional[int] = Field(default=None, gt=0)
    max_concurrency: Optional[int] = Field(default=None, gt=0)
    rate_limit_per_minute: Optional[int] = Field(default=None, gt=0)
    max_response_bytes: Optional[int] = Field(
        default=None, gt=0,
        description="Largest response/output size this capability may return, in bytes.",
    )
    max_record_count: Optional[int] = Field(
        default=None, gt=0,
        description="Largest number of records (rows, items, documents) one invocation may "
        "produce or process, e.g. a dataset extraction's row count.",
    )
    max_cost_usd: Optional[float] = Field(
        default=None, gt=0,
        description="Largest estimated model/inference cost, in USD, one invocation may incur. "
        "See f.libraries.retention.models.estimate_cost_usd for the caller-side estimate this "
        "is compared against.",
    )


class ScheduledHealthPolicy(BaseModel):
    """Opt-in bounds for metadata-driven live capability health checks."""

    enabled: bool = False
    cron: str = Field(default="0 0 * * * *", min_length=1)
    timezone: str = Field(default="UTC", min_length=1)
    max_samples_per_run: int = Field(default=1, ge=1, le=100)
    max_data_bytes: int = Field(default=1_000_000, ge=1, le=100_000_000)
    max_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    rate_limit_per_minute: int = Field(default=1, ge=1, le=1000)
    escalate_after_failures: int = Field(default=3, ge=1, le=100)

    @field_validator("cron")
    @classmethod
    def _six_field_windmill_cron(cls, value: str) -> str:
        if len(value.split()) != 6:
            raise ValueError("scheduled health cron must use Windmill's six-field syntax")
        return value


class CapabilityMetadata(BaseModel):
    """Agent-selection metadata for one discoverable capability."""

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema MAJOR.MINOR this record was written against.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Windmill path this metadata describes, e.g. "
        "f/capabilities/collection/web_fetch. Windmill remains authoritative "
        "for the script's own argument schema — this only points at it.",
    )
    capability_version: str = Field(..., min_length=1)
    summary: str = Field(
        ...,
        min_length=1,
        description="One-line, agent-facing description used for discovery/search.",
    )
    maturity: CapabilityMaturity
    deterministic: bool = Field(
        default=True,
        description="Whether identical inputs are expected to produce identical outputs. "
        "AI/model-backed capabilities must set this false.",
    )
    owners: list[str] = Field(..., min_length=1)
    effects: CapabilityEffects = Field(default_factory=CapabilityEffects)
    autonomy: AutonomyPolicy = Field(default_factory=AutonomyPolicy)
    limits: CapabilityLimits = Field(default_factory=CapabilityLimits)
    scheduled_health: ScheduledHealthPolicy = Field(default_factory=ScheduledHealthPolicy)
    test_requirements: list[str] = Field(
        default_factory=list,
        description="Paths/ids of promotion-gating tests this capability must pass, "
        "e.g. under windmill/tests/contracts/ (HF-015).",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Other capability paths this one depends on (HF-012 impact analysis "
        "traverses these).",
    )

    @field_validator("dependencies")
    @classmethod
    def _cannot_depend_on_self(cls, v: list[str], info) -> list[str]:
        path = info.data.get("path")
        if path is not None and path in v:
            raise ValueError("a capability cannot list itself in dependencies")
        return v


def main() -> dict:
    """Self-test / demo: export both models' JSON Schemas."""
    return {
        "CapabilityMetadata": CapabilityMetadata.model_json_schema(),
        "AutonomyPolicy": AutonomyPolicy.model_json_schema(),
    }
