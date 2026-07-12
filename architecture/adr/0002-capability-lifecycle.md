# 0002 — Capability lifecycle: candidate before mutation

**Status:** Proposed
**Related:** [HF-003](https://github.com/ryancollingwood/HermesFlow/issues/41), [HF-010](https://github.com/ryancollingwood/HermesFlow/issues/48), [HF-013](https://github.com/ryancollingwood/HermesFlow/issues/51), `docs/plans/hermesflow-lifecycle.md`

## Context

Hermes can compose, generate, and repair Windmill capabilities. Active or
scheduled capabilities must not be edited in place by an agent, or a bad
generation can silently break shared workflows with no review step.

## Decision

Capabilities move through `REQUESTED -> SEARCH -> COMPOSE/REUSE/GENERATE ->
CANDIDATE -> TESTED -> ACTIVE -> EXECUTE -> SUCCEEDED/FAILED -> INSPECT ->
PATCH -> TEST -> PROMOTE`. New bounded read-only candidates may be created,
tested, and executed automatically; promotion to active, schedule changes,
secret/resource changes, and destructive operations require policy-gated
approval.

**Autonomy schema implemented (HF-003).** `CapabilityMetadata` and
`AutonomyPolicy` are Pydantic models in
`windmill/f/libraries/capability/models.py`, importable as `from
f.libraries.capability.models import CapabilityMetadata, AutonomyPolicy,
AutonomyAction` (same `f/libraries/<topic>/models.py` pattern as HF-002's
lineage schemas). This is the schema half of this ADR; the evaluator that
reads it (HF-010) and the promotion flow that enforces it (HF-013) are still
open. Key design points:

- **`AutonomyPolicy` has one field per lifecycle action** — `discover`,
  `execute`, `compose`, `create_candidate`, `modify_candidate`, `promote`,
  `schedule` — each an `automatic` / `approval_required` `AutonomyLevel`.
  `discover` through `modify_candidate` default `automatic` (bounded,
  candidate-namespace-only actions, per the paragraph above); `promote` and
  `schedule` default `approval_required`.
- **`promote` and `schedule` are structurally pinned to
  `approval_required`** — a field validator rejects any attempt to construct
  an `AutonomyPolicy` with either set to `automatic`, full stop. This isn't a
  per-capability default that a "low risk" label could override: there is no
  field combination (maturity, effects, or otherwise) that can produce an
  automatic-promotion or automatic-schedule policy.
  `windmill/tests/test_capability_models.py` sweeps every
  `CapabilityMaturity` value against an all-effects-off, otherwise-automatic
  policy and confirms the validator still rejects it — a low-risk label
  alone cannot imply promotion or scheduling permission.
- **`CapabilityEffects`** covers exactly `network`, `filesystem`, `database`,
  `external`, each a plain boolean.
- **`CapabilityMetadata`** deliberately does *not* duplicate anything
  Windmill's own `*.script.yaml` already owns (argument schema, resource
  types) — `path` just points at the script. It carries only what Windmill
  has no concept of: `summary` (discovery), `maturity`, `owners`,
  `effects`, `autonomy`, `limits` (timeout/concurrency/rate bounds),
  `test_requirements` (promotion-gating test references for HF-015), and
  `dependencies` (for HF-012's impact analysis; self-dependency is rejected).
- **Two worked examples** in the test suite: a read-only web-fetch capability
  (`effects.network=True`, everything else `False`) and a write capability
  that upserts product snapshots (`effects.database=True`) — both still
  `execute=automatic` (routine execution of already-active, reviewed code is
  not what's gated) with `promote`/`schedule` forced to
  `approval_required` regardless.
- JSON Schemas exported to
  `docs/schemas/{capability_metadata,autonomy_policy}.schema.json`,
  drift-checked against the live models the same way HF-002 checks the
  lineage schemas.

**Policy evaluator implemented (HF-010).** `evaluate_policy()` in
`f/hermes_flow/policies/evaluator.py` takes a `PolicyContext` (the
requested action, the target capability's `CapabilityMetadata` if known,
and request-time facts — requested concurrency/rate, a caller-supplied
`destructive` flag) and returns a `PolicyDecision`
(`automatic`/`approval_required`/`denied`, with a reason). This is
deliberately a *different* question from `AutonomyPolicy` itself:
`AutonomyPolicy` is the capability's static declared default; the
evaluator combines that default with request-time context to reach a
decision that can only be as permissive as the default, never more so.
Fail-closed rules, in priority order: `discover` is always `automatic`
(searching/listing has no side effects); any other action against an
**unknown** capability path is `denied`, not `approval_required` — there's
nothing safe to default to, and "approval_required" implies something
reviewable exists, which it doesn't for an unknown path; `promote`/
`schedule` are always `approval_required` for a known capability, matching
`AutonomyPolicy`'s structural invariant; a request flagged `destructive`
escalates an otherwise-`automatic` decision to `approval_required` (never
further, and never de-escalates an already-gated action); a request whose
declared concurrency/rate needs exceed the capability's own
`CapabilityLimits` is `denied` outright, not routed to approval. 45 tests
cover every action against a representative context set, including a
table-driven sweep.

This still leaves the promotion *workflow* (HF-013: the actual approval
flow that acts on an `approval_required` decision) undesigned.

## Status

Autonomy schema ([HF-003](https://github.com/ryancollingwood/HermesFlow/issues/41))
and policy evaluator ([HF-010](https://github.com/ryancollingwood/HermesFlow/issues/48))
both implemented and done. Still pending the promotion workflow
([HF-013](https://github.com/ryancollingwood/HermesFlow/issues/51)) before
this ADR can be marked Accepted.
