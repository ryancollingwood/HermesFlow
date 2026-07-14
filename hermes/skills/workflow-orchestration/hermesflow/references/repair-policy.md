# Repair policy

How to react when an `ExecutionResult` comes back `failure` or `partial`,
following
[`architecture/adr/0002-capability-lifecycle.md`](../../../../../architecture/adr/0002-capability-lifecycle.md)'s
`... -> EXECUTE -> SUCCEEDED/FAILED -> INSPECT -> PATCH -> TEST ->
PROMOTE` tail.

**Status note:** HF-029's read-only failure-inspection package exists at
`f/hermes_flow/repair/inspection`; it returns the bounded, redacted repair
context documented in
[`docs/failure-inspection.md`](../../../../../docs/failure-inspection.md).
HF-030's `f/hermes_flow/repair/generate_candidate` now turns a complete context
into a policy-checked candidate with exact generation provenance; see
[`docs/repair-candidate-generation.md`](../../../../../docs/repair-candidate-generation.md).
HF-031's `f/hermes_flow/repair/promote_fixture` and
`f/hermes_flow/testing/source_drift_fixture` promote sanitised source artifacts
into candidate-only regressions; see
[`docs/source-drift-regression-fixtures.md`](../../../../../docs/source-drift-regression-fixtures.md).
HF-032's bounded orchestrated retry is not built yet. Inspection, generation,
or fixture promotion therefore does not authorize active promotion or a retry loop.

## 1. INSPECT — read what actually happened before acting

Start from the `ExecutionResult`:

- **Structured inspection** — for a submitted failed Windmill job, run
  `f/hermes_flow/repair/inspection` with the job ID and versioned catalogue.
  Treat its classification as advisory evidence, preserve its original-job
  link, and surface any truncation or collection warnings before proposing a
  repair.

- **`failure_summary`** — read it as written; don't paraphrase it away
  (SKILL.md Rule 4 / `result-presentation.md`). It should already be
  actionable if the capability that produced it followed
  `generation-policy.md`.
- **`job`**, if present — pull `getJobLogs` via the `windmill` MCP toolset
  for the full trace. A `failure_summary` is a summary; the job logs are
  the source of truth when the summary alone doesn't explain enough to act.
- **No `job` present** — the task never got far enough to submit one (e.g.
  Windmill was unreachable, per ADR 0001's failure behavior). There's
  nothing to inspect on the Windmill side; the fix is retrying once
  Windmill is confirmed reachable again, not generating a repair candidate
  for code that never ran.
- **`partial`** — check `warnings` for which part didn't fully succeed.
  Don't treat a partial result as "close enough" without understanding
  specifically what's missing from it.

## 2. Decide: retry, patch, or stop and ask

- **Retry as-is** — appropriate when the failure looks transient
  (Windmill was briefly unreachable, a rate limit, a timeout against a
  flaky upstream) and nothing about the capability's own code caused it.
  Retry once; if it fails again the same way, treat that as a real problem
  rather than bad luck and move to PATCH or stop-and-ask.
- **PATCH** — appropriate when the job logs point at an actual bug in the
  capability's own code (a real example already on record: `runScriptByPath`
  submitting a job with an unresolved `conn: hermes_endpoint` argument,
  producing `TypeError: 'NoneType' object is not subscriptable` —
  ADR 0005's Consequences). A patch is generated code and follows
  `generation-policy.md` in full. Run `f/hermes_flow/repair/generate_candidate`
  only with the complete HF-029 context and current versioned catalogue. A
  rejected, stale, truncated, or incomplete context means stop and surface the
  reason; never hand-edit around the gate. A valid repair is a new
  **candidate**, never a direct edit to the active capability, carries its own updated
  `CapabilityMetadata`, and needs its own `TEST` pass before `PROMOTE` —
  the fact that it's "just a fix" doesn't shortcut Rule 3.

  When source drift supplied the failure evidence, promote the failed source
  artifact with `f/hermes_flow/repair/promote_fixture`, select it together with
  any previous baseline fixture, and run both through the HF-016 regression
  selector against the isolated candidate. Never point the fixture runner at an
  active path or retain unsanitised source content in a manifest.
- **Stop and ask** — appropriate whenever the failure implies something
  the task's original request didn't account for (a missing resource, an
  ambiguous requirement, a capability that doesn't do what its `summary`
  claimed), or after a patch attempt doesn't resolve it. Looping silently
  on repeated failures burns time and produces noisy job history without
  getting closer to a working result; surface what you've learned and let
  the user decide the next step.

## 3. Report, even when you stop partway

If you inspect, decide not to patch, and stop to ask the user — that's
still a result worth reporting via `ExecutionResult`/`render_summary()`,
not a silent pause. Report the original failure, what you found inspecting
it, and the specific question or decision you need from the user, so
they're not left reconstructing your reasoning from the raw job logs
themselves.
