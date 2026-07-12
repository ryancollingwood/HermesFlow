---
name: hermesflow
description: Orchestrate HermesFlow tasks — select or generate a capability, run it exclusively through Windmill, and present the result. Use for any task that needs code to run, not just conversation.
version: 0.1.0
author: Ryan Philip Collingwood
license: MIT
metadata:
  hermes:
    tags: [hermesflow, orchestration, windmill, capability, lifecycle, autonomy]
    related_skills: [data-platform-add-pipeline]
---

# HermesFlow Orchestration

Route a task through the HermesFlow lifecycle: decide whether it needs
execution at all, prefer an existing capability over writing new code,
never mutate active code directly, run everything through Windmill, and
present the result honestly — including when it fails.

## Architecture (tl;dr)

HermesFlow separates *deciding what to do* (Hermes: this skill) from
*doing it* (Windmill: the only place task code runs). Capabilities move
through a lifecycle — `REQUESTED -> SEARCH -> COMPOSE/REUSE/GENERATE ->
CANDIDATE -> TESTED -> ACTIVE -> EXECUTE -> ... -> PROMOTE` — where new or
changed code always lands in a **candidate** namespace first and never
touches what's already active without an explicit promotion step. Three
Pydantic schemas make this machine-checkable rather than a convention to
remember: `CapabilityMetadata`/`AutonomyPolicy`
(`f/libraries/capability/models.py`) describe what a capability is allowed
to do unattended, and `ExecutionResult` (`f/libraries/results/models.py`)
is what you report back.

## When to Use

- Any request that needs code to run — fetch/transform/store data, call an
  external service, do anything with a side effect.
- Deciding whether an existing capability already covers a request, or a
  new one needs to be composed/generated.
- Presenting the outcome of a Windmill execution back to the user.
- Handling a failed or partially-succeeded execution.

**Don't use for:** pure conversation — answering a question, explaining
code, drafting text — where nothing needs to execute. That's still allowed
without touching Windmill at all (see Rule 1 below); this skill's
selection/execution/presentation machinery simply doesn't apply to it.

> The canonical, human-authored version of the result envelope's
> presentation rules is
> [`docs/result-envelope-rendering.md`](../../../../docs/result-envelope-rendering.md)
> in the repo; `references/result-presentation.md` mirrors it for
> MCP-driven use but can drift out of date. If the two conflict, the repo
> doc wins — flag the discrepancy rather than silently following whichever
> one you read first (same rule `data-platform-add-pipeline` uses for its
> own canonical docs).

---

## Rule 1 — Windmill is the only place task code runs

**Never execute task code directly** — no shell, no local Python, no
browser automation, no ad hoc filesystem writes as a substitute for a real
capability — regardless of what tools are technically available in the
session. This is not a style preference; it's
[`architecture/adr/0001-windmill-exclusive-execution.md`](../../../../architecture/adr/0001-windmill-exclusive-execution.md)'s
decision, stated here at the prompt level per that ADR's own Consequences
section.

**The one exception:** pure conversation — reasoning, explaining,
summarizing, answering, drafting text — never needs Windmill at all. That's
not a workaround for this rule; it's the rule correctly not applying,
because nothing is being executed on the user's behalf.

**If Windmill is unreachable:** say so plainly and stop. Do not fall back
to direct execution "just this once" to get the task done anyway, no
matter how simple the task looks. Report an `ExecutionResult` with
`outcome=failure` and an actionable `failure_summary` (e.g. "Windmill is
unavailable — retry once it's back"); `job` will legitimately be absent,
since nothing was submitted.

**Transport:** talk to Windmill through the `windmill` MCP server (native
Streamable-HTTP/SSE, decided in
[`architecture/adr/0005-hermes-windmill-transport.md`](../../../../architecture/adr/0005-hermes-windmill-transport.md)) —
`listScripts`/`getScriptByPath` to inspect a capability, `runScriptByPath`
to execute one, `getJob`/`getJobLogs` to inspect what happened. A session
scoped to just this toolset uses `hermes chat -t windmill`. Known
constraint: `runScriptByPath`'s MCP schema doesn't pass resource-typed
arguments (e.g. a `conn: hermes_endpoint` parameter) — a capability that
needs one will fail with the resource unresolved. Don't route around this
by trying a different execution path; it's a capability-design constraint
(design the capability's signature to avoid resource-typed args, or use a
direct Windmill job-run REST call for that specific case) documented in
ADR 0005's Consequences.

## Rule 2 — Primitives before workflows before generation

When a task needs a capability, search **in this order** and stop at the
first thing that covers it:

1. **An existing primitive** — a small, single-purpose capability that
   already does exactly this.
2. **An existing workflow** — a composition of primitives that already does
   this end to end, even if no single primitive does.
3. **Generate new code** — only when neither exists. This always produces a
   **candidate**, never a direct edit to active code (Rule 3).

Reaching for generation before ruling out reuse produces duplicate,
undertested capabilities and is the failure mode this ordering exists to
prevent. See `references/capability-selection.md` for how to search given
what's actually built today (the full searchable catalogue is
[HF-008](https://github.com/ryancollingwood/HermesFlow/issues/46)/[HF-009](https://github.com/ryancollingwood/HermesFlow/issues/47),
not yet implemented) and how to read a candidate's `CapabilityMetadata`
before deciding to reuse it.

## Rule 3 — Candidate before mutation

New or changed capability code always goes to a **candidate** path first —
never a direct edit to something already active. This holds regardless of
how small or "obviously safe" the change looks.
[`architecture/adr/0002-capability-lifecycle.md`](../../../../architecture/adr/0002-capability-lifecycle.md)
covers the full lifecycle; `references/generation-policy.md` covers what
must accompany a new candidate before it's usable, and
`references/repair-policy.md` covers the INSPECT → PATCH → TEST loop after
a failure.

**Promotion is never automatic, structurally.** `AutonomyPolicy.promote`
and `.schedule` (`f/libraries/capability/models.py`) can only ever be
`approval_required` — a validator rejects any attempt to construct the
opposite, for every capability regardless of maturity or effects. Don't
present a candidate as "ready to go live" or take a promotion action
yourself; surface it to the user as a decision for them to make.

## Rule 4 — Report what actually happened

Build an `ExecutionResult` (`f/libraries/results/models.py`) for every task
that reached Windmill, and render it with `render_summary()` (or by hand,
following the same rules) rather than freeform prose. The schema itself
won't let a `windmill_job` result claim `success`/`partial` without a real
job reference — if you don't have one, the outcome isn't success. See
`references/result-presentation.md` for the full presentation rules,
worked examples for success/partial/failure, and what never to do (never
paraphrase away a `failure_summary`, never hide a failed run's job
reference behind a "show details" toggle).

---

## Reference Documents

- `references/capability-selection.md` — the primitives → workflows →
  generation search order in practice, given what's built so far; how to
  read `CapabilityMetadata` (maturity, effects, autonomy, dependencies)
  before choosing to reuse a capability.
- `references/generation-policy.md` — when generating new code is allowed,
  what a new candidate must carry (owners, effects, test requirements) and
  why, and the autonomy defaults that apply to it.
- `references/repair-policy.md` — handling a failed or partial
  `ExecutionResult`: when to retry, when to generate a repair candidate,
  when to stop and ask the user instead of looping.
- `references/result-presentation.md` — mirrors
  [`docs/result-envelope-rendering.md`](../../../../docs/result-envelope-rendering.md)
  (canonical) for MCP-driven use.
