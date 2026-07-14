# Automatic rollback recommendation

HF-034 provides `f/hermes_flow/candidate_ops/rollback_recommendation`, layered
on top of HF-014's rollback and HF-020's scheduled health tracking. It does
not replace either: `recommend_rollback` decides *whether* a rollback is
warranted, and `execute_approved_rollback` is the only step that writes
anything, delegating the actual restore to HF-014's `rollback_capability`.

## Recommendation

`recommend_rollback(catalogue_yaml, capability_path)` is read-only. It:

1. loads the capability's current HF-020 `HealthState` and refuses to
   recommend anything if there is no evidence for the active version yet, or
   if the capability is currently healthy (`consecutive_failures == 0`);
2. compares `consecutive_failures` against the capability's own
   `scheduled_health.escalate_after_failures` threshold;
3. once the threshold is met, fetches the `HealthFailureRecord` for every
   failing run in the current streak — each lives at a deterministic,
   run-count-keyed variable path (see `save_failure` in
   `f.hermes_flow.testing.scheduled_health`), so no listing endpoint is
   needed — and classifies each one with HF-029's deterministic
   `classify_failure`;
4. recommends a rollback only when the streak is **not** exclusively
   classified as `infrastructure` (connection refused, timeouts, DNS, 5xx,
   etc.). A run of failures that is entirely a transient external outage does
   not justify rolling back working code, so the recommendation withholds
   itself and says why. A streak that mixes infrastructure noise with any
   other category (a real code defect, source drift, a missing dependency) is
   still treated as evidence of a genuine regression.

The `RollbackRecommendation` this returns is the whole point of "compares
current and previous evidence": `current_evidence` is the latest failing run,
`previous_evidence` is every earlier run in the same streak, and
`transient_only` records exactly why a threshold-meeting streak was or was
not accepted as more than noise. It also carries `required_tests`,
`affected_workflows`, `affected_schedules`, and `has_side_effects` (from the
capability's own `CapabilityEffects`) so nothing about blast radius is
hidden — `requires_approval` is always `true`; this module never executes a
rollback on its own.

## Approved execution

`execute_approved_rollback` refuses to run unless:

- the supplied recommendation actually has `recommended: true` — this
  module only acts on capabilities it has itself endorsed, never on a
  caller's unilateral say-so;
- `approval_granted` is `true` **and** `approved_by` names an authenticated
  approver (an unapproved rollback is recorded as `approval_rejected`, not
  raised, exactly like an unapproved HF-032 retry);
- whenever the recommendation shows side effects, affected workflows, or
  affected schedules, the caller has explicitly set
  `acknowledge_side_effects=True` after reviewing them. This is the "never
  silent" guarantee: a rollback that could disrupt a schedule or a
  side-effecting capability cannot proceed on approval alone.

Once past those gates it calls HF-014's `rollback_capability` — the failed
version is never deleted, only superseded by a new Windmill version pointing
back at the restored content, exactly as HF-014 already guarantees — and
then reruns the capability's impacted contract/smoke tests (HF-016's
dependency-aware regression selection) against the now-restored active
version. A capability with no impacted tests short-circuits verification as
trivially passed rather than reporting a false failure. If verification
fails, the rollback write is **not** undone; the outcome is recorded as
`verification_failed` with a note that manual follow-up is required, since
repeatedly reverting is adaptive-repair's job (HF-032), not this module's.

Every outcome — `approval_rejected`, `rollback_succeeded`, or
`verification_failed` — is persisted as a `RollbackExecutionRecord` under
`f/hermes_flow_state/rollback_recommendation/`, alongside HF-014's own
rollback provenance variable.

The recommendation contract is checked in at
`docs/schemas/rollback_recommendation.schema.json`.
