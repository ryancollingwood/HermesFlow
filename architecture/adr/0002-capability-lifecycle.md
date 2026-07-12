# 0002 — Capability lifecycle: candidate before mutation

**Status:** Proposed
**Related:** [HF-003](https://github.com/ryancollingwood/HermesFlow/issues/41), [HF-010](https://github.com/ryancollingwood/HermesFlow/issues/48), [HF-013](https://github.com/ryancollingwood/HermesFlow/issues/51), `docs/plans/hermesflow-lifecycle.md`

## Context

Hermes can compose, generate, and repair Windmill capabilities. Active or
scheduled capabilities must not be edited in place by an agent, or a bad
generation can silently break shared workflows with no review step.

## Decision (to be detailed)

Capabilities move through `REQUESTED -> SEARCH -> COMPOSE/REUSE/GENERATE ->
CANDIDATE -> TESTED -> ACTIVE -> EXECUTE -> SUCCEEDED/FAILED -> INSPECT ->
PATCH -> TEST -> PROMOTE`. New bounded read-only candidates may be created,
tested, and executed automatically; promotion to active, schedule changes,
secret/resource changes, and destructive operations require policy-gated
approval. This stub will be filled in with the concrete evaluator and
promotion-flow design as HF-010/HF-013 land.

## Status

Stub — full decision record to follow once the policy evaluator ([HF-010](https://github.com/ryancollingwood/HermesFlow/issues/48))
and promotion workflow ([HF-013](https://github.com/ryancollingwood/HermesFlow/issues/51))
are implemented.
