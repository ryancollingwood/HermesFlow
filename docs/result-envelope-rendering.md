# Presenting `ExecutionResult` to a user

This is the human-presentation guidance for
`windmill/f/libraries/results/models.py`'s `ExecutionResult` — how Hermes
should turn one machine-readable result into what a user actually reads.
`render_summary()` in that module is the reference implementation these
rules describe; `windmill/tests/test_result_models.py` snapshot-tests its
output for the success/partial/failure cases below.

**This doc is destined for
`hermes/skills/workflow-orchestration/hermesflow/references/result-presentation.md`**
once [HF-005](https://github.com/ryancollingwood/HermesFlow/issues/43)
creates that skill — see
[`docs/plans/hermesflow-lifecycle.md`](plans/hermesflow-lifecycle.md) for
status. Until then this is the canonical copy; HF-005 should fold it in
rather than rewrite it from scratch.

## The one rule that overrides all formatting preferences

**Never present a `windmill_job` result as a success or partial success
without showing its Windmill job reference.** `ExecutionResult` enforces
this at the schema level (a `success`/`partial` `windmill_job` result
literally cannot be constructed without a `job`), so if you're rendering a
valid `ExecutionResult`, the job reference is guaranteed to exist for those
outcomes — never omit it from the presented text to save space. It's the
one piece of evidence a user (or a later debugging session) can use to go
look at what actually happened, independent of anything Hermes says about
it.

## Per-outcome presentation

- **`success`** — lead with a clear positive signal. Show what ran (and its
  version, if the user might care which code executed), the job reference,
  duration, and any artifacts produced. Keep it short — a success doesn't
  need justification.
- **`partial`** — lead with a clear *not-fully-successful* signal, distinct
  from both success and failure at a glance (don't bury "partial" in prose).
  Show the same fields as success, **plus warnings**, and make sure the
  warnings explain *which part* didn't fully succeed — a partial result
  without an explanation of what's missing is worse than a plain failure,
  because it invites false confidence.
- **`failure`** — lead with a clear negative signal. Always show
  `failure_summary` — it's schema-required and must be actionable (what
  broke, and where possible what to do about it — "retry once Windmill is
  back" beats "an error occurred"). Show the job reference **if present**;
  its absence is itself informative (the task never got far enough to
  submit a job — e.g. Windmill was unreachable per
  `architecture/adr/0001-windmill-exclusive-execution.md`'s failure
  behaviour) and shouldn't be presented as a rendering bug.
- **`conversational`** — no job, no workflow path, nothing execution-shaped
  to show. Render as a plain answer; don't manufacture execution-flavoured
  framing ("ran successfully") for something that never touched Windmill.

## Capability changes and dependencies on other guidance

If `capability_changes` is non-empty, call it out distinctly from ordinary
artifacts — a user should never have to infer "this task also modified the
capability catalogue" from a generic artifact list. Once
[HF-013](https://github.com/ryancollingwood/HermesFlow/issues/51)'s
promotion workflow exists, a `promoted` change is exactly the kind of thing
a user will want to see foregrounded, not buried after warnings.

## What NOT to do

- Don't paraphrase or summarize `failure_summary` into vaguer language —
  it was written to be actionable; compressing it away defeats the point.
- Don't hide the job reference behind a "show details" affordance for
  `failure`/`partial` outcomes. Those are exactly the cases where a user is
  most likely to need it immediately.
- Don't invent additional outcome states in presentation ("mostly
  succeeded", "succeeded with caveats") that don't map back to
  `ResultOutcome`. If the nuance matters, it belongs in `warnings` or
  `failure_summary`, not a new ad hoc label.
