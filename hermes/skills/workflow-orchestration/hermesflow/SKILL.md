---
name: hermesflow
description: Orchestrate HermesFlow tasks — search for or generate a capability, run it exclusively through Windmill, and present the result. Use for any task that needs code to run, including natural-language product collection, comparison, price research, or multi-source shopping requests.
version: 0.4.0
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

**This rule is stated at the prompt level and, on its own, is advisory
only — an unscoped session can and will reach for a built-in tool anyway**
(confirmed by [HF-006](https://github.com/ryancollingwood/HermesFlow/issues/44)'s
live testing: asked to fetch a URL "directly, don't overthink it" with no
toolset restriction, the model called the built-in `web_extract` tool
despite this rule). **A HermesFlow session must be started with a
restricted toolset, not just this skill preloaded:**

```sh
hermes chat -t windmill,hermesflow,memory,todo,clarify,session_search -s hermesflow
# or, non-interactively:
hermes chat -Q -t windmill,hermesflow,memory,todo,clarify,session_search -s hermesflow -q "..."
```

`-t` is a session-scoped **allowlist**, not additive to whatever's globally
enabled — a session started this way has *only* those tools, confirmed by
listing available tools inside such a session and by directly attempting
shell, browser, filesystem, and Python execution, all of which report the
tool as simply not present rather than being declined. **Do not** reach
for `hermes tools disable <name>` to achieve this instead — that changes
the *global* `cli` platform config, permanently removing the tool from
every Hermes session (including ordinary, non-HermesFlow assistant use,
where those tools are legitimate). Scope the *session*, not the
installation.

**Known gap:** the `delegation` toolset (spawning sub-agent tasks) is
deliberately excluded from the list above rather than resolved — a
delegated sub-agent's own toolset scoping is an open question this hasn't
addressed. Don't enable `delegation` in a HermesFlow session until that's
worked out; a sub-agent with unrestricted tools would silently reopen this
exact gap one level down.

**If Windmill is unreachable:** say so plainly and stop. Do not fall back
to direct execution "just this once" to get the task done anyway, no
matter how simple the task looks. Report an `ExecutionResult` with
`outcome=failure` and an actionable `failure_summary` (e.g. "Windmill is
unavailable — retry once it's back"); `job` will legitimately be absent,
since nothing was submitted. Under the restricted toolset above this is
also structural, not just a request you're honoring: there is no other
tool available to fall back to.

**Transport:** talk to Windmill through the `windmill` MCP server (native
Streamable-HTTP/SSE, decided in
[`architecture/adr/0005-hermes-windmill-transport.md`](../../../../architecture/adr/0005-hermes-windmill-transport.md)) —
`listScripts`/`listFlows` to search, `getScriptByPath`/`getFlowByPath` to
inspect a capability, `runScriptByPath`/`runFlowByPath` to execute an
argument-free one, and
`getJob`/`getJobLogs` to inspect what happened. Always inspect a flow before
running it so its schema—not a guessed argument shape—is authoritative. Known
constraint: the native `runScriptByPath`/`runFlowByPath` MCP schemas do not
accept job arguments. Do not pretend an argumented capability ran by submitting
it empty. HF-028's product flow uses the separately registered, one-purpose
`hermesflow` MCP tool `run_product_collection`; it validates and fixes the
target, resource, bounds, and read-only settings before calling Windmill. Other
argumented capabilities need an equally narrow approved transport or a signature
that avoids inputs, as documented in ADR 0005's Consequences.

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
prevent. See `references/capability-selection.md` for the live catalogue/search
surface and how to read `CapabilityMetadata` before deciding to reuse an asset.

### Product-collection exemplar

For natural-language product research, collection, price comparison, or
multi-source shopping requests, read
`references/product-collection-exemplar.md` and follow its contract:

> The canonical human guide is
> [`docs/hermesflow-conversational-exemplar.md`](../../../../docs/hermesflow-conversational-exemplar.md).
> If it conflicts with the bundled reference, the repo doc wins and the drift
> must be flagged.

1. Classify the requested outcome before selecting anything. A one-off read,
   comparison, or report is supported; purchasing, changing prices, deleting
   products, or mutating a source system is not.
2. **Always search Windmill** (`listFlows`, and `listScripts` only if a
   primitive might satisfy the whole request) before naming a capability.
   Inspect the selected asset with `getFlowByPath`. A remembered path is not a
   substitute for search, even when the likely match is
   `f/workflows/product_collection`.
3. Ask one focused clarification and do not execute if source URLs or whether
   the user wants a one-off run versus a schedule are ambiguous. An explicit
   HTTP(S) URL makes its exact hostname allowlist unambiguous: derive that
   hostname without asking. Ask only when the user requests a broader parent or
   redirect domain without naming it. Scheduling remains approval-required.
4. Evaluate the selected capability's execute policy and bounds. The active
   product collection flow may execute automatically for a non-destructive,
   one-off request within its limits; its internal snapshot persistence does
   not turn the user's read-only intent into permission to mutate a source.
5. Run the inspected flow with the `hermesflow` MCP tool
   `run_product_collection`, then retrieve the completed job with Windmill's
   `getJob`. Never use native `runFlowByPath` for this argumented flow, and never
   replace it with direct fetching or local code.
6. Explain the selected workflow in one user-facing sentence. Do not recite all
   internal primitives unless asked. Report the flow path/version, Windmill job
   reference, outcome/warnings, and artifact references from the returned
   result.

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
- `references/product-collection-exemplar.md` — exact intent classification,
  search, clarification, policy, execution arguments, and response contract for
  HF-028's natural-language product collection exemplar.
