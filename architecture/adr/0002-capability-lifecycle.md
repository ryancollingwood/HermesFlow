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

This still leaves the policy *evaluator* (HF-010: how a capability's
metadata plus request context decides discover/execute/etc. at runtime) and
the promotion *workflow* (HF-013: the actual approval flow) undesigned.

## Status

Autonomy schema implemented ([HF-003](https://github.com/ryancollingwood/HermesFlow/issues/41),
done). Still pending the policy evaluator ([HF-010](https://github.com/ryancollingwood/HermesFlow/issues/48))
and promotion workflow ([HF-013](https://github.com/ryancollingwood/HermesFlow/issues/51))
before this ADR can be marked Accepted.
