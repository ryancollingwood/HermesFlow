# 0004 — AI steps inside Windmill workflows are explicit and inspectable

**Status:** Proposed
**Related:** [HF-019](https://github.com/ryancollingwood/HermesFlow/issues/57), `docs/plans/hermesflow-lifecycle.md`

## Context

Workflows may call Hermes for classification, extraction, schema-guided
generation, or ambiguous branching. These calls are nondeterministic and must
not be presented or treated as if they were plain deterministic steps.

## Decision (to be detailed)

All such calls go through one standard wrapper
(`f/libraries/ai/invoke_hermes_structured`, extending the existing
`f/hermes/client.py` pattern) that captures prompt, conversation context,
input payload, output schema, model/parameters, parsed output, raw output
artifact, model metadata, token usage, and retries — and is marked
nondeterministic in capability metadata. The platform promises execution
reproducibility (same code, inputs, prompt, and configuration can be
rerun), not bitwise-identical model outputs. This stub will be filled in with
the concrete wrapper contract as HF-019 lands.

## Status

Stub — full decision record to follow once the structured invocation wrapper
([HF-019](https://github.com/ryancollingwood/HermesFlow/issues/57)) is
implemented.
