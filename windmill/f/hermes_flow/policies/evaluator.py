"""
Policy evaluator — path: f/hermes_flow/policies/evaluator

Other scripts import these directly:

    from f.hermes_flow.policies.evaluator import PolicyContext, PolicyOutcome, evaluate_policy

(same import pattern as `f.hermes_flow.catalogue.models`.) Deterministic —
no LLM. `evaluate_policy()` decides, for one requested action against one
capability, whether it's `automatic`, `approval_required`, or `denied`.

This is not the same question `AutonomyPolicy` (`f.libraries.capability.models`)
answers. `AutonomyPolicy` is a capability's own *static, declared default*
for each action — set once, by whoever authors the capability's metadata.
`evaluate_policy()` combines that default with *request-time context*
(is the capability even known? does this specific request exceed the
capability's own declared limits? has the caller flagged this particular
invocation as destructive?) to produce the actual decision for one request
— which can only ever be as permissive as the declared default, never more
so. A capability whose `AutonomyPolicy.execute` is `automatic` can still
have a specific execute request come back `denied` (unknown capability,
limits exceeded) or `approval_required` (flagged destructive); the reverse
never happens — nothing here can turn a declared `approval_required` into
`automatic`.

Fail-closed rules, in priority order:
1. `discover` is always `automatic` — it only searches/lists the catalogue
   (see `f.hermes_flow.catalogue.search`), which has no side effects
   regardless of what capability the caller is looking for.
2. Any other action against a capability path with **no** `CapabilityMetadata`
   (`context.capability is None`) is `denied` — an unknown capability's
   effects, limits, and autonomy policy are all unknown, so there is
   nothing safe to default to. This is deliberately stricter than
   defaulting to `approval_required`: an approval flow at least implies
   someone can review *what* they're approving, which isn't possible here.
3. `promote` and `schedule` are always `approval_required` for a known
   capability — matching `AutonomyPolicy`'s own structural invariant
   (`architecture/adr/0002-capability-lifecycle.md`). They are never
   `automatic`, and not `denied` either: they're always a real,
   available action, just always gated.
4. A request flagged `destructive=True` is escalated at least one step
   from whatever the capability's own policy says: `automatic` becomes
   `approval_required`. (An already-`approval_required` action stays
   `approval_required` — there is nowhere further to escalate to below
   `denied`, and a human review step already covers it.)
5. A request whose `requested_concurrency`/`requested_rate_per_minute`/
   `requested_duration_seconds`/`requested_response_bytes`/
   `requested_record_count`/`requested_cost_usd` exceeds the capability's own
   declared `CapabilityLimits` is `denied` — exceeding a declared bound is a
   violation to reject, not a risk to route for approval. The last four
   (HF-035) are the retention/cost dimensions in
   `f.libraries.capability.models.CapabilityLimits`; like concurrency and
   rate, a limit that's merely *declared* but not *requested* for this call
   is not itself a violation — there's nothing to compare.
6. Otherwise, the decision is exactly the capability's own
   `AutonomyPolicy.level_for(action)`.

Running THIS script directly evaluates one context (passed as JSON via the
`context_json` argument — same Windmill-jobs-have-no-repo-filesystem
reasoning as `f.hermes_flow.catalogue.models`) and returns the decision,
doubling as an integration test that this module's logic runs correctly
inside Windmill's actual Python environment.
"""
import json
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from f.libraries.capability.models import AutonomyAction, AutonomyLevel, CapabilityMetadata

SCHEMA_VERSION = "1.0"


class PolicyOutcome(str, Enum):
    automatic = "automatic"
    approval_required = "approval_required"
    denied = "denied"


class PolicyContext(BaseModel):
    """The request-time facts a policy decision needs, beyond the capability's own metadata."""

    schema_version: str = Field(default=SCHEMA_VERSION)
    action: AutonomyAction
    capability: Optional[CapabilityMetadata] = Field(
        default=None,
        description="The target capability's metadata, if known. None means the "
        "requested path has no catalogue entry — fails closed for every action "
        "except discover.",
    )
    requested_concurrency: Optional[int] = Field(default=None, ge=1)
    requested_rate_per_minute: Optional[int] = Field(default=None, ge=1)
    requested_duration_seconds: Optional[int] = Field(default=None, ge=1)
    requested_response_bytes: Optional[int] = Field(default=None, ge=0)
    requested_record_count: Optional[int] = Field(default=None, ge=0)
    requested_cost_usd: Optional[float] = Field(default=None, ge=0)
    destructive: bool = Field(
        default=False,
        description="Caller-supplied flag for a specific invocation known to be "
        "destructive (e.g. deletes data) beyond what the capability's own declared "
        "effects capture. Escalates automatic to approval_required; never de-escalates.",
    )


class PolicyDecision(BaseModel):
    schema_version: str = Field(default=SCHEMA_VERSION)
    action: AutonomyAction
    capability_path: Optional[str] = None
    outcome: PolicyOutcome
    reason: str = Field(..., min_length=1)


def evaluate_policy(context: PolicyContext) -> PolicyDecision:
    action = context.action

    if action is AutonomyAction.discover:
        return PolicyDecision(
            action=action,
            capability_path=context.capability.path if context.capability else None,
            outcome=PolicyOutcome.automatic,
            reason="discover only searches/lists the catalogue — no side effects, always automatic",
        )

    if context.capability is None:
        return PolicyDecision(
            action=action,
            capability_path=None,
            outcome=PolicyOutcome.denied,
            reason="no CapabilityMetadata for this path — failing closed, nothing safe to default to",
        )

    capability = context.capability

    if action in (AutonomyAction.promote, AutonomyAction.schedule):
        return PolicyDecision(
            action=action,
            capability_path=capability.path,
            outcome=PolicyOutcome.approval_required,
            reason=f"{action.value} always requires approval (architecture/adr/0002-capability-lifecycle.md)",
        )

    base_level = capability.autonomy.level_for(action)

    if context.destructive and base_level is AutonomyLevel.automatic:
        return PolicyDecision(
            action=action,
            capability_path=capability.path,
            outcome=PolicyOutcome.approval_required,
            reason=f"request flagged destructive — escalated from {base_level.value} to approval_required",
        )

    limits = capability.limits
    bounded_requests = (
        ("concurrency", context.requested_concurrency, limits.max_concurrency, ""),
        ("rate", context.requested_rate_per_minute, limits.rate_limit_per_minute, "/min"),
        ("duration", context.requested_duration_seconds, limits.timeout_seconds, "s"),
        ("response size", context.requested_response_bytes, limits.max_response_bytes, " bytes"),
        ("record count", context.requested_record_count, limits.max_record_count, ""),
        ("cost", context.requested_cost_usd, limits.max_cost_usd, " USD"),
    )
    for label, requested, limit, unit in bounded_requests:
        if requested is not None and limit is not None and requested > limit:
            return PolicyDecision(
                action=action,
                capability_path=capability.path,
                outcome=PolicyOutcome.denied,
                reason=f"requested {label} {requested}{unit} exceeds capability limit {limit}{unit}",
            )

    outcome = PolicyOutcome(base_level.value)
    return PolicyDecision(
        action=action,
        capability_path=capability.path,
        outcome=outcome,
        reason=f"capability's own autonomy policy for {action.value} is {base_level.value}",
    )


def main(context_json: str) -> dict:
    """Self-test / integration check: evaluate one PolicyContext given as JSON text."""
    context = PolicyContext.model_validate(json.loads(context_json))
    decision = evaluate_policy(context)
    return decision.model_dump(mode="json")
