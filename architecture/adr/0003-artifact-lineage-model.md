# 0003 — Artifact lineage model

**Status:** Proposed
**Related:** [HF-002](https://github.com/ryancollingwood/HermesFlow/issues/40), [HF-017](https://github.com/ryancollingwood/HermesFlow/issues/55), [HF-018](https://github.com/ryancollingwood/HermesFlow/issues/56), `docs/plans/hermesflow-lifecycle.md`

## Context

Every workflow run needs a traversable chain from raw source through
intermediate transformations to final outputs, so results can be inspected
and reproduced after the fact.

## Decision (to be detailed)

Every run carries a standard execution context envelope (trace/parent trace,
conversation/request IDs, capability/version, initiating actor) and produces
artifact references (raw/intermediate/final stage, content hash, storage URI,
creator capability/version, derivation links). Windmill retains logs and
small results; larger or independently queryable artifacts live in the
content-addressed artifact filesystem, collection Postgres, or Parquet. This
stub will be filled in with the concrete schema and storage adapter design as
HF-002/HF-017/HF-018 land.

## Status

Stub — full decision record to follow once the execution context/artifact
schemas ([HF-002](https://github.com/ryancollingwood/HermesFlow/issues/40))
and the storage adapter ([HF-017](https://github.com/ryancollingwood/HermesFlow/issues/55))
are implemented.
