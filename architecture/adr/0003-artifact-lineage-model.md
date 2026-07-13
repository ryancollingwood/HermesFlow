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

## Decision

Every run carries a standard execution context envelope (trace/parent trace,
conversation/request IDs, capability/version, initiating actor) and produces
artifact references (raw/intermediate/final stage, content hash, storage URI,
creator capability/version, derivation links). Windmill retains logs and
small results; larger or independently queryable artifacts live in the
content-addressed artifact filesystem, collection Postgres, or Parquet.

**Schemas implemented (HF-002).** `ExecutionContext` and `ArtifactRef` are
Pydantic models in `windmill/f/libraries/lineage/models.py`, importable as
`from f.libraries.lineage.models import ExecutionContext, ArtifactRef,
ArtifactStage` (same pattern as `f.hermes.client`) — the first content in the
new `f/libraries/` namespace, added to `wmill.yaml`'s `includes:` wholesale
(`f/libraries/**`, no runtime-state subfolder to carve out, unlike
`f/data_platform`). Key design points:

- **`schema_version` (MAJOR.MINOR string) on both models**, with a stated
  compatibility rule: within a MAJOR version only additive, optional fields
  may be introduced — never remove, rename, or narrow a required field.
  Pydantic's default behaviour (ignore unrecognized fields) gives forward
  compatibility for free; `windmill/tests/test_lineage_models.py` has
  explicit tests for both directions (an older-shaped payload missing newer
  optional fields, and a newer payload carrying a field this version doesn't
  know about).
- **`ExecutionContext.parent_trace_id`** links a child execution to its
  parent's `trace_id`; a validator rejects an execution naming itself as its
  own parent.
- **`ArtifactRef.stage`** is an enum of exactly `raw` / `intermediate` /
  `final`. **`content_hash`** is validated as a 64-character hex SHA-256
  digest (normalized to lowercase) — this is the hash HF-017's storage
  adapter will use for the content-addressed path under
  `${SHARED_DIR}/artifacts/` (HF-000A). **`derived_from`** is a list of
  upstream `artifact_id`s, validated against self-reference; a raw artifact
  has an empty list, and a chain (raw → transformation → final report) is
  exercised end to end in the test suite.
- **JSON Schema export for docs/CI**: `models.py`'s own `main()` exports
  both schemas (`ExecutionContext.model_json_schema()` /
  `ArtifactRef.model_json_schema()`) and is deployed as a normal Windmill
  script — running it doubles as a live self-test. The checked-in copies
  under `docs/schemas/{execution_context,artifact_ref}.schema.json` are
  asserted to match the live models in
  `windmill/tests/test_lineage_models.py`, so a schema change that isn't
  re-exported fails CI rather than drifting silently.
- **Tests live in `windmill/tests/`, not under `windmill/f/`** — deliberately
  outside `wmill.yaml`'s sync scope (so they're never pushed to the server as
  spurious scripts) and outside the CI script↔metadata↔lock parity check
  (which only walks `windmill/f/`). `windmill/conftest.py` makes
  `f.<folder>.<module>` importable the same way Windmill's own runtime
  resolves those relative imports. `make test` / `make ci` run them locally;
  CI runs them in the `python` job.

**Storage adapter implemented (HF-017).**
`f/libraries/storage/artifacts.py` stores immutable content at
`/shared/artifacts/<sha256[:2]>/<sha256>` and per-lineage-event envelopes at
`/shared/artifacts/metadata/<artifact_id>.json`. Duplicate bytes reuse the
same object while retaining separate ArtifactRefs and metadata. Writes are
bounded and atomic; reads verify canonical containment, SHA-256, size, and
reject traversal and symlink escapes. `ArtifactRef` gained additive optional
`size_bytes` and `media_type` fields for backward compatibility; the adapter
always populates both.

Still open for HF-018: context-propagation helpers that stitch a flow's steps
into one lineage graph. Retention policy remains a hardening concern.

## Status

Schemas implemented ([HF-002](https://github.com/ryancollingwood/HermesFlow/issues/40),
done) and the artifact mount provisioned
([HF-000A](https://github.com/ryancollingwood/HermesFlow/issues/37), done).
The storage adapter is implemented
([HF-017](https://github.com/ryancollingwood/HermesFlow/issues/55), done).
Still pending lineage/context-propagation helpers
([HF-018](https://github.com/ryancollingwood/HermesFlow/issues/56)) before
this ADR can be marked Accepted.
