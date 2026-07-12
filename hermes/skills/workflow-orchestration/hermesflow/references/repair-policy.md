# Repair policy

How to react when an `ExecutionResult` comes back `failure` or `partial`,
following
[`architecture/adr/0002-capability-lifecycle.md`](../../../../../architecture/adr/0002-capability-lifecycle.md)'s
`... -> EXECUTE -> SUCCEEDED/FAILED -> INSPECT -> PATCH -> TEST ->
PROMOTE` tail.

**Status note:** the automated repair orchestration this describes the
manual shape of —
[HF-029](https://github.com/ryancollingwood/HermesFlow/issues/67) through
[HF-032](https://github.com/ryancollingwood/HermesFlow/issues/70)
(failure-inspection package, repair-candidate generation, drift fixtures,
orchestrated retry with attempt limits) — isn't built yet. Until it is,
every step below is something *you* do deliberately in the conversation,
not something a repair loop does for you. Don't invent retry-loop behavior
that doesn't exist yet; follow the steps once, then stop and report.

## 1. INSPECT — read what actually happened before acting

Start from the `ExecutionResult`:

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
  `generation-policy.md` in full: it's a new/modified **candidate**, never
  a direct edit to the active capability, carries its own updated
  `CapabilityMetadata`, and needs its own `TEST` pass before `PROMOTE` —
  the fact that it's "just a fix" doesn't shortcut Rule 3.
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
