# 0003 — Artifact lineage model

**Status:** Proposed
**Related:** [HF-000A](https://github.com/ryancollingwood/HermesFlow/issues/37), [HF-002](https://github.com/ryancollingwood/HermesFlow/issues/40), [HF-017](https://github.com/ryancollingwood/HermesFlow/issues/55), [HF-018](https://github.com/ryancollingwood/HermesFlow/issues/56), `docs/plans/hermesflow-lifecycle.md`

## Context

Every workflow run needs a traversable chain from raw source through
intermediate transformations to final outputs, so results can be inspected
and reproduced after the fact.

**Artifact root provisioned (HF-000A).** `${SHARED_DIR}/artifacts/`
(`/shared/artifacts` inside containers) is the mount the content-addressed
store will live under. It needed no new Docker volume: `${SHARED_DIR}` is
already bind-mounted into `hermes`, `windmill_server`, `windmill_worker`, and
`windmill_worker_native` (`docker-compose.yml`), the same reuse the
data-platform raw layer already relies on for `${SHARED_DIR}/datalake/`
(`docs/plans/datalake.md`). `make init`/`install.py` create the `artifacts/`
subdir and `make fix-permissions`/`install.py`'s chown step cover it as part
of the existing recursive `${SHARED_DIR}` ownership fix — no separate
permission plumbing. Verified live: a Windmill job (`jobs/run/preview`)
wrote and re-read a file under `/shared/artifacts/`, and the file survived a
`docker restart` of the worker container. The storage *adapter* (hashing,
path scheme, retention) is still HF-017's to design — this only provisions
and proves the mount it will sit on.

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
