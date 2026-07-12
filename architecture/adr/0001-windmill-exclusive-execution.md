# 0001 — Windmill is the exclusive execution boundary for task code

**Status:** Accepted
**Related:** [HF-001](https://github.com/ryancollingwood/HermesFlow/issues/39), `docs/plans/hermesflow-lifecycle.md`

## Context

HermesFlow pairs Hermes (conversational intent, planning, capability discovery
and repair) with Windmill (script/flow execution, versioning, logs, retries,
secrets). Hermes agents generally have tool access that *could* run code
directly — shell, local Python, browser automation, filesystem writes — either
through built-in tools or ad hoc MCP servers. If any of those paths are used
for task work, the platform loses the properties it exists to provide:
visible code, reproducible executions, and a single authoritative run record.

## Decision

**All executable task code runs through Windmill. Hermes never executes task
code locally, and never silently falls back to shell, Python, browser, or
filesystem tools to accomplish task work.**

### Responsibilities

| Component | Owns | Must not own |
|---|---|---|
| Hermes | Intent interpretation, planning, capability discovery, composition, candidate generation, failure analysis, result explanation. | Direct task-code execution; authoritative logs; secret storage; active-code mutation. |
| Windmill | All code execution — scripts, flows, versions, schedules, jobs, retries, logs, resources, secrets, approvals. | Conversational intent management; long-form user interaction. |
| External stores (Postgres, artifact filesystem, Parquet, Baserow/Directus) | Durable raw/intermediate/final artifacts and structured datasets. | Execution scheduling or policy decisions. |

### Exceptions

Non-executable conversation is unaffected by this rule: Hermes may reason,
explain, summarize, answer questions, and draft text without touching
Windmill at all. The boundary applies specifically to *task code execution* —
anything that reads/writes files, calls the network for task purposes, or
mutates state on the user's behalf.

### Failure behaviour when Windmill is unavailable

If Windmill cannot be reached, Hermes must:
1. Not fall back to direct execution to complete the task anyway.
2. Tell the user plainly that Windmill is unavailable and the task cannot run
   until it is back.
3. Continue to be usable for non-executable conversation in the meantime.

There is no silent degradation path. A failed or unavailable Windmill means a
failed or deferred task, communicated as such.

## Consequences

- Every executed task has a Windmill job reference; see [HF-004](https://github.com/ryancollingwood/HermesFlow/issues/42)
  (result envelope, done) for how that reference is surfaced.
  `ExecutionResult` in `windmill/f/libraries/results/models.py` enforces this
  structurally — a `windmill_job` result cannot validate as `success` or
  `partial` without a `WindmillJobRef` — rather than leaving it to
  convention. The one exception is `execution_type=conversational`, matching
  this ADR's own non-executable-conversation exception above.
- Enforcement requires auditing and restricting Hermes's own execution-capable
  tools for HermesFlow sessions — tracked separately as
  [HF-006](https://github.com/ryancollingwood/HermesFlow/issues/44).
- This ADR is a prerequisite for the HermesFlow orchestration skill
  ([HF-005](https://github.com/ryancollingwood/HermesFlow/issues/43)), which
  states and enforces this principle at the prompt level.

## Verification

- Reviewed against three scenarios: normal execution (task runs via Windmill,
  job reference returned), Windmill unavailable (explicit refusal, no
  fallback), and an AI-only conversational answer (no Windmill interaction
  required or attempted).
- A reviewer can determine unambiguously, from this document alone, where
  code executes for any given request.
