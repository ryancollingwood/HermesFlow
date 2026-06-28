# Data Platform Plan (Revised for HermesFlow stack)

Status: **validated end-to-end** against the live stack — deployed to
Windmill, extraction and dbt build both ran successfully, real rows landed
in `collection_db.data_platform`. See "Validated end-to-end" below for the
bugs found/fixed during validation. Outstanding: flow definition, README/
AGENTS.md docs pass, and the deferred Hermes MCP access (see
"Implementation steps").

Revises the original "Data Platform MVP Plan" to fit HermesFlow's existing
Windmill + Postgres + `collection_db` schema-isolation conventions.

## Architecture

```
Source APIs (REST/GraphQL/webhooks/files)
    │
    ▼
dlt pipeline (Windmill worker, per-job ephemeral DuckDB)
    │
    ├──► /shared/datalake/<source>/<date>/*.parquet   (immutable raw layer,
    │                                                   bind-mounted, same
    │                                                   ${SHARED_DIR} volume
    │                                                   already used by
    │                                                   hermes + workers)
    │
    └──► DuckDB (in-process, transient, one per worker/job — staging only,
          no shared file, no locking contention across jobs)
              │
              ▼  dbt-duckdb cleans/types staging models
              │
              ▼  dbt writes final mart models to:
    collection_db (Postgres) — new `data_platform` schema
              │
              ├───► (later) Hermes MCP read-only access — DEFERRED
              └───► other consumers query Postgres directly
```

## Component decisions

| Layer | Component | Rationale |
|---|---|---|
| Extract + Load | dlt, inside Windmill Python jobs | No new infra; Windmill already runs Python jobs via uv |
| Raw immutable layer | Parquet under `${SHARED_DIR}/datalake/<source>/<date>/` | Reuses the existing `/shared` bind mount already wired into `hermes` and Windmill workers (`docker-compose.yml`) — no new volume |
| Staging | DuckDB, **ephemeral, one instance per Windmill job** | Sidesteps DuckDB's single-writer file-lock problem entirely — no shared `.duckdb` file across jobs/workers |
| Transform | dbt-duckdb (staging) → dbt-postgres (mart) | Mart materializes directly into Postgres, avoiding a second persistent database engine for the durable target |
| Final store | `collection_db` Postgres, new `data_platform` schema | Matches existing isolation pattern (see `collection_db/initdb/01-init.sh`: `baserow`, `directus`, `collection` schemas, dedicated roles) |
| Serving/Viz | — (Rill dropped for now) | Desktop-only app doesn't fit the server-side, reverse-proxied stack; can run locally against Postgres later if wanted |
| Worker image | Default Windmill worker, no custom Dockerfile | Windmill resolves per-script deps via uv/cached `py_runtime`; avoids extra image-build/maintenance surface |
| Agent access | Deferred | Will eventually need a read-only MCP server over the `data_platform` schema, following the Baserow/Directus MCP pattern (Hermes never runs raw SQL — `AGENTS.md`). Not solving this now. |

## Why this differs from the original plan

- **DuckDB stays, but only as a per-job staging engine**, not a shared
  platform-wide file. The original plan's single `platform.duckdb` file
  with `busy_timeout` PRAGMA mitigations is unnecessary complexity once
  each job gets its own instance — there's nothing to contend over.
- **Postgres replaces DuckDB as the durable mart target.** `collection_db`
  is already the stack's general-purpose shared store; growth here is
  expected and matches how Directus/Baserow data already lives.
- **Parquet raw layer is kept** (same immutability/replay rationale as the
  original plan) but lives under the *existing* shared volume rather than
  assuming a new `/shared/raw` host mount.
- **Rill and the custom worker Dockerfile are dropped** for this iteration
  — neither fits cleanly today (Rill is desktop-only; Windmill already
  handles per-script dependencies without a custom image).
- **MCP-based Hermes access is explicitly deferred** rather than designed
  now, consistent with "don't solve the last mile yet."

## Proposed Postgres schema isolation (collection_db)

New role + schema, following the existing `baserow`/`directus`/`collection`
pattern in `collection_db/initdb/01-init.sh`:

```sql
CREATE ROLE data_platform LOGIN PASSWORD '${DATA_PLATFORM_DB_PASSWORD}';
CREATE SCHEMA IF NOT EXISTS data_platform AUTHORIZATION data_platform;
ALTER ROLE data_platform SET search_path TO data_platform, public;
```

dbt's `profiles.yml` (postgres adapter) connects as this role, writing only
into the `data_platform` schema — isolated from `baserow`, `directus`, and
`collection`.

## Directory structure (revised)

```
data-platform/
├── dlt/
│   ├── pipelines/               ← one .py per source
│   └── requirements.txt
├── dbt/
│   ├── dbt_project.yml          ← profile: postgres (mart), duckdb (staging) via two targets, or two dbt projects
│   ├── profiles.yml
│   ├── models/
│   │   ├── staging/             ← dbt-duckdb, reads Parquet from /shared/datalake
│   │   └── marts/               ← dbt-postgres, writes to collection_db.data_platform
│   └── tests/
└── windmill/
    └── scripts/                 ← deployed Windmill flow scripts
```

(No `rill/` directory, no `worker/Dockerfile`, no platform-wide `.duckdb`
file — all dropped per the decisions above.)

## dbt-duckdb ↔ Postgres bridge — decided

Single dbt project, single DuckDB engine. Staging models read Parquet from
`/shared/datalake`; mart models write through dbt-duckdb's Postgres attach
feature so the durable target is still `collection_db`, with no separate
mart-only dbt project:

```yaml
# dbt profiles.yml (duckdb adapter)
data_platform:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ':memory:'        # ephemeral per Windmill job — no shared file
      attach:
        - path: "postgres://data_platform:${DATA_PLATFORM_DB_PASSWORD}@collection_db:5432/{{ env_var('COLLECTION_DB_NAME') }}"
          type: postgres
          alias: pg
```

Mart models target the `pg` attached database (`{{ target.database }}.pg...`
or a dbt `postgres` custom materialization pointing at the `pg` alias) so
`CREATE TABLE`/`MERGE` for marts lands in `collection_db.data_platform`,
while staging/intermediate models stay in the in-memory DuckDB instance and
never persist past the job.

## Schema/role setup — done

`collection_db/initdb/01-init.sh` only runs on a **fresh** Postgres data
volume, and the volume already exists, so:

1. Generated `DATA_PLATFORM_DB_PASSWORD` and appended it to `.env`
   (gitignored, same as the other `*_DB_PASSWORD` vars).
2. Ran the `CREATE ROLE data_platform` / `CREATE SCHEMA data_platform
   AUTHORIZATION data_platform` SQL directly against the live
   `collection_db` container (one-off, matching what the init script does
   on fresh init). Verified via `\du`/`\dn`.
3. Updated `collection_db/initdb/01-init.sh` to create the same role/schema,
   and added `DATA_PLATFORM_DB_PASSWORD` to the `collection_db` service's
   `environment:` block in `docker-compose.yml`, so a future fresh-volume
   bootstrap creates it automatically without a manual step.

## Windmill resources — done

Resources/variables in this repo are YAML files under `windmill/f/<folder>/`,
synced via `wmill` (see `windmill/wmill.yaml`'s `includes`, currently scoped
to `f/hermes/**` and `f/collection/**` only — untracked folders get wiped on
push, so this needs an explicit addition). Pattern to follow:
`windmill/f/collection/collection_db.resource.yaml` +
`windmill/f/collection/db_password.variable.yaml`.

For this pipeline:

1. **New folder `windmill/f/data_platform/`**, added to `includes` in
   `windmill/wmill.yaml`. Done.
2. **`data_platform_db.resource.yaml`** — built-in `postgresql` resource
   type, `host: collection_db`, `user: data_platform`, `dbname: collection`,
   `password: $var:f/data_platform/db_password`. No custom resource type
   needed. Done.
3. **`db_password.variable.yaml`** — secret variable placeholder
   (`is_secret: true`); `make windmill-push` now also creates/updates the
   real `f/data_platform/db_password` secret in Windmill's store from
   `DATA_PLATFORM_DB_PASSWORD` in `.env`, mirroring the existing
   `f/collection/db_password` block. Done (Makefile updated) — actually
   pushing to a live Windmill server is a `make windmill-push` run away,
   not yet executed.
4. The dlt/dbt Windmill scripts (`extract_hn_stories`, `dbt_run`) reference
   this resource as their `db` parameter, same as existing collection
   scripts do. Done.

## Hermes — no wiring needed yet

Hermes today is only wired as a job *trigger*/LLM endpoint — Windmill calls
Hermes (`f/hermes/local.resource`), not the reverse. There is no agent-facing
wiring to build for the pipeline to run end-to-end.

The deferred item (read-only Hermes access to `data_platform`) is an MCP
registration step, not a Windmill concern. When picked up later, it follows
the same template as `make baserow-mcp` in the `Makefile`: provision a
read-only Postgres MCP server, bake an `mcp-remote`-style bridge into the
`hermes` image (as already done for Baserow in `hermes/Dockerfile`), then
`hermes mcp add data_platform --command ... --transport ...`. Not part of
this implementation pass.

## Implementation steps

1. ~~Add `data_platform` role + schema to `collection_db` (one-off SQL +
   init script update)~~ — done.
2. ~~Create `data-platform/` directory structure~~ — done:
   `data-platform/dlt/pipelines/hn_stories.py` (example source — Hacker News
   top stories, with the `_extracted_at`/`_job_id`/`_pipeline`/`_batch_id`/
   `_source_url` provenance columns), `data-platform/dbt/` (staging model
   reading Parquet from `/shared/datalake` via DuckDB, mart model written
   through the Postgres attach into `collection_db.data_platform`, with
   exhaustive column descriptions on the mart per the agent-data-contract
   convention).
3. ~~Create the Windmill resource + variable files~~ — done:
   `windmill/f/data_platform/{folder.meta,data_platform_db.resource,
   db_password.variable}.yaml`, `wmill.yaml` includes updated.
4. ~~Minimal dlt pipeline writing Parquet to `${SHARED_DIR}/datalake/...`~~ —
   done (`hn_stories.py`, writes to `/shared/datalake/hn_stories/<date>/`).
5. ~~dbt project: staging (DuckDB) → mart (Postgres via attach)~~ — done,
   see step 2.
6. ~~Wrap as Windmill scripts~~ — done: `f/data_platform/extract_hn_stories`
   and `f/data_platform/dbt_run`. **Flow definition not created** — Windmill
   flow YAML wasn't authored from scratch without an in-repo example to
   mirror; chain the two scripts manually in the Windmill UI (Flows →
   extract → dbt run) once deployed, or ask for this explicitly as a
   follow-up.
7. **New compose change**: `data-platform/` is bind-mounted read-only into
   `windmill_worker` at `/data_platform` (`${DATA_PLATFORM_DIR:-./data-platform}`
   in `docker-compose.yml`) so scripts can `import` the dlt pipeline code and
   point dbt at `--project-dir /data_platform/dbt` without a custom worker
   image. `.env.example` documents the override var and
   `DATA_PLATFORM_DB_PASSWORD`.
8. ~~Deploy to the live Windmill instance~~ — done: `make windmill-push`
   ran, plus a direct `wmill sync push` for lock-file fixes (see below). The
   `f/data_platform/db_password` secret is set in Windmill's variable store.
9. **Not yet done**: README/AGENTS.md documentation pass (originally step 7).
10. (Deferred, separate pass) Hermes MCP read-only access to `data_platform`
    — see above.

## Validated end-to-end

Ran `extract_hn_stories` → `dbt_run` (command `build`) against the live
stack and confirmed real rows in `collection_db.data_platform.mart_hn_stories`,
owned by the `data_platform` role. Re-ran extraction a second time and
confirmed no duplicate rows in the mart (story count stayed 1:1 with
distinct story ids) despite the raw Parquet layer being append-only.

Fixes made during validation (all already applied to the files above, not
separate pending work):

- **`windmill-push`'s secret-variable creation was silently failing** for
  *all three* existing variables (`f/hermes/api_key`, `f/collection/db_password`,
  and the new `f/data_platform/db_password`) — Windmill's
  `variables/create` API requires a `description` field; without it the
  create call 400s and the Makefile's fallback `update` call only succeeds
  if the variable already exists from a prior manual creation. Fixed all
  three `curl` calls in the `Makefile` to include `description`.
- **Hand-written `.script.lock` files with only top-level pins broke at
  runtime** (`ModuleNotFoundError` for `typing_extensions`, then
  `pkg_resources`) — Windmill's `uv pip install` from a lock file needs the
  full resolved dependency tree, not just direct dependencies. Regenerated
  both lock files with `uv pip compile`, pinning `setuptools<81` for
  `extract_hn_stories` (newer setuptools dropped `pkg_resources`, which
  `dlt`'s optional dbt helper still imports) and swapping `dbt-duckdb==1.9.1`
  (yanked from PyPI) for `1.10.1` in `dbt_run`.
- **`/data_platform` is mounted read-only**, but dbt wants to write
  `logs/`/`target/` inside the project dir — added `--target-path
  /tmp/dbt_target --log-path /tmp/dbt_logs` to the `dbtRunner` invocation in
  `dbt_run.py`.
- **dbt-core's default `generate_schema_name` macro concatenates
  `<default_schema>_<custom_schema>`** (e.g. a model with `schema:
  data_platform` under a profile with `schema: stg` became
  `stg_data_platform`) instead of using the configured schema standalone —
  this also explains the original `dbt_project.yml` `+database: pg` config
  appearing to do nothing (model-level `database`/`schema` config and the
  macro's prefixing happen independently). Added
  `data-platform/dbt/macros/generate_schema_name.sql` overriding it to
  return the custom schema as-is, and moved the mart's `database`/`schema`
  override into an explicit `{{ config(...) }}` call in
  `mart_hn_stories.sql` rather than relying on `dbt_project.yml`. Also added
  an explicit `schema: stg` to `profiles.yml`'s `dev` target — without one,
  dbt's startup schema-bootstrap step used an implicit name that resolved
  against the attached Postgres `pg` alias, where the `data_platform` role
  has rights on its own schema but not database-level `CREATE`.
- **dbt-duckdb's external-source syntax is `meta.external_location`**, not
  the top-level `external:` key I'd originally written in `sources.yml` —
  fixed, and the glob narrowed to `**/stories/*.parquet` to match dlt's
  actual nested-table layout (dlt splits arrays like a story's `kids` into
  a sibling `stories__kids` table/folder).
- **Raw layer is `append`-only by design, so staging needs to dedupe** —
  the original `stg_hn_stories.sql` passed rows through 1:1; running
  `extract_hn_stories` more than once would have multiplied mart rows.
  Added a `row_number() over (partition by id order by _extracted_at desc)`
  dedupe in staging so the mart stays idempotent across repeated
  extractions. Also fixed the dlt resource's `write_disposition` from
  `merge` (which the filesystem/Parquet destination doesn't support — dlt
  silently fell back to `append` with a warning) to `append` explicitly,
  matching the immutable-raw-layer design intent.
- **`dbt_run`'s default command changed from `run` to `build`** — staging
  models live in an ephemeral in-memory DuckDB scoped to a single job
  process, so a separate `test` job afterwards has nothing to check against
  for staging (mart tests still pass since marts persist in Postgres).
  `build` runs+tests in one process and is now the script default.
