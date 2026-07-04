# Adding a new data platform pipeline

This is the step-by-step checklist for adding a new dlt → dbt → Postgres
pipeline to the `data_platform` stack (see [docs/plans/datalake.md](plans/datalake.md)
for the architecture and rationale). Follow it in order — most of the
failure modes below were hit and fixed once already; skipping a step
reproduces them.

The worked example throughout is the existing `hn_stories` pipeline
(`data-platform/dlt/pipelines/hn_stories.py` → `data-platform/dbt/models/
staging/stg_hn_stories.sql` → `.../marts/mart_hn_stories.sql`). Copy it as a
starting point rather than writing from scratch.

## 0. Know the architecture in one paragraph

dlt extracts and writes **immutable, append-only Parquet** to
`/shared/datalake/<pipeline>/<date>/` (the `${SHARED_DIR}` bind mount, same
one Hermes and Windmill workers already use). dbt-duckdb then runs in an
**ephemeral, in-memory** DuckDB instance — one per Windmill job, never a
shared file — staging models read the raw Parquet directly, and mart
models write through to `collection_db`'s `data_platform` Postgres schema
via dbt-duckdb's Postgres `attach` feature. The whole `data-platform/`
directory is bind-mounted **read-only** into Windmill workers at
`/data_platform`; there is no custom worker image.

## 1. Write the dlt pipeline

Add `data-platform/dlt/pipelines/<name>.py`, following `hn_stories.py`'s
shape: a `dlt.resource` generator yielding dicts, plus the provenance
columns every row should carry — `_extracted_at`, `_job_id`, `_pipeline`,
`_batch_id`, `_source_url` (see [docs/plans/datalake.md](plans/datalake.md)
for why). A `run(datalake_dir, ...)` function that builds the
`dlt.destinations.filesystem(...)` pipeline and calls `.run(...,
loader_file_format="parquet")`, plus a `if __name__ == "__main__":` block
for local testing.

**Use `write_disposition="append"`, not `"merge"`.** The
filesystem/Parquet destination doesn't support merge strategies — dlt
silently falls back to append with a warning if you set it anyway. The raw
layer is append-only by design; don't fight that.

Add any new third-party imports to `data-platform/dlt/requirements.txt`
*only* if every pipeline needs them — pipeline-specific extras go in the
Windmill script's own lock file (step 4), not here (this file isn't
installed by Windmill at all; it's just for local dev/testing of the dlt
code directly).

## 2. Write the dbt staging model

Add `data-platform/dbt/models/staging/sources.yml` entry (or a new
`sources.yml` if grouping differently) pointing at the raw Parquet:

```yaml
sources:
  - name: datalake
    tables:
      - name: <pipeline>
        meta:
          external_location: "read_parquet('/shared/datalake/<pipeline>/**/<table>/*.parquet')"
```

**The key is `meta.external_location`, not a top-level `external:` key** —
that's dbt-duckdb-specific syntax, easy to get wrong by analogy with other
adapters. Match the glob to dlt's actual on-disk layout: dlt splits nested
arrays/objects into sibling tables (e.g. `stories` and `stories__kids`), so
point the glob at the specific table's subfolder, not the pipeline root.
Check what actually landed with `find` on the host's `${SHARED_DIR}/datalake`
before writing the glob if you're not sure.

Add `models/staging/stg_<name>.sql` — clean/rename/type the raw columns,
**and dedupe**:

```sql
with ranked as (
    select *, row_number() over (partition by <natural_key> order by _extracted_at desc) as rn
    from {{ source('datalake', '<pipeline>') }}
)
select * exclude (rn) from ranked where rn = 1
```

This is not optional. The raw layer is append-only, so re-running
extraction accumulates one row per record per run; without the dedupe, the
mart row count multiplies every time the pipeline runs. Write the column
descriptions in a matching `stg_<name>.yml` (test `not_null` on the natural
key at minimum).

## 3. Write the dbt mart model

Add `models/marts/mart_<name>.sql`. **Put the cross-database target in an
explicit `{{ config(...) }}` call in the model file**, not in
`dbt_project.yml`'s `+database:`/`+schema:` — the project-level config
doesn't reliably take effect for dbt-duckdb's attach feature (see
[docs/plans/datalake.md](plans/datalake.md) "Validated end-to-end" for what
went wrong when we tried it):

```sql
{{ config(database='pg', schema='data_platform') }}

select ...
from {{ ref('stg_<name>') }}
```

Write `mart_<name>.yml` with **exhaustive column descriptions** — this
file is the agent-facing data contract (Hermes reads it via MCP once that
access is wired up), not just dbt documentation. Every column needs a
description a human or agent could act on without reading the SQL. Add
`unique` + `not_null` tests on the primary key.

You do **not** need to touch `data-platform/dbt/macros/generate_schema_name.sql`
or `profiles.yml`'s `schema: stg` default — both already make custom
schema names resolve standalone instead of dbt-core's default
`<default_schema>_<custom_schema>` concatenation. If you ever rename the
default schema or add a second attached database, re-read the relevant
"Validated end-to-end" entries in the datalake plan first.

## 4. Add the Windmill extract script

Add `windmill/f/data_platform/extract_<name>.py`, mirroring
`extract_hn_stories.py`: `sys.path.insert(0, "/data_platform/dlt/pipelines")`,
import your pipeline module, call its `run(...)`.

Add the matching `.script.yaml` (summary, schema for any parameters,
`lock: '!inline f/data_platform/extract_<name>.script.lock'`) and the lock
file itself.

**Do not hand-write the lock file with just top-level package pins, and do
not trust `wmill generate-metadata` to populate it correctly.** Both will
break at runtime — generate-metadata's static import analysis only sees
imports it can resolve from the script file itself (it missed `dlt`
entirely here, since the actual `import dlt` lives in the bind-mounted
pipeline module, not the Windmill script). Generate the full resolved
dependency tree with `uv`:

```sh
uv pip compile --python-version 3.12 - <<'EOF' > /tmp/lock.txt
dlt[duckdb,parquet]==1.6.1
requests==2.32.3
setuptools<81
EOF
```

The `setuptools<81` pin is currently required for any pipeline that
imports `dlt` — newer setuptools dropped `pkg_resources`, which one of
dlt's optional code paths still imports at module load time. Drop it once
upstream dlt fixes that, not before. Then turn the compiled output into the
lock format (strip comments, keep `# py: 3.12` as line 1):

```sh
{ echo "# py: 3.12"; grep -E '^[a-zA-Z0-9_.\-]+(\[[a-z,]+\])?==' /tmp/lock.txt; } > windmill/f/data_platform/extract_<name>.script.lock
```

**Resource handling: prefer `wmill.get_resource()` over an injected
parameter for fixed, dedicated resources.** `dbt_run` originally took
`db: postgresql` as an injected/bound parameter (the standard Windmill
pattern, and still the right call for a script meant to run against a
*caller-chosen* resource). But `f/data_platform/data_platform_db` is the
*only* resource this pipeline ever attaches to — there's nothing for a
caller to legitimately choose. Fetch it directly instead:

```python
from wmill import get_resource

db = get_resource("f/data_platform/data_platform_db")
```

This closes off an entire failure mode: a run can no longer reach the
function body with a missing/null/empty `db` (e.g. from a flow step or
schedule that didn't bind the parameter correctly) — `get_resource()` fails
fast with its own clear error instead of an opaque `KeyError`/`TypeError`
three lines into `main()`. Drop the `db: postgresql` parameter and its
`TypedDict` entirely when you do this; don't leave a vestigial unused
parameter in the schema.

## 5. Decide: new dbt_run, or reuse the existing one

If your pipeline's staging/mart models live in the same
`data-platform/dbt/` project (the default — one project, one DuckDB
instance, one Postgres attach), you don't need a new Windmill script for
the transform step. The existing `f/data_platform/dbt_run` already runs
`dbt build` against the whole project, which will pick up your new models
automatically (it has no model-name filtering today — see "Future work"
below if you need selective runs).

## 6. Push to Windmill

```sh
make windmill-push
```

This pushes everything under `f/data_platform/**` (per `wmill.yaml`
includes) and creates/updates the `f/data_platform/db_password` secret
from `.env`. If you only changed lock files (not resources/folders), a
plain `wmill sync push --yes` from `windmill/` is enough and faster.

**Known gotcha:** `make windmill-push` (and `install.sh`/`install.py`) runs
`wmill generate-metadata` across the *entire* `windmill/` tree first, not
just your new folder. If any existing script (in `f/hermes/` or
`f/collection/`) has an import inside a function body rather than at module
level, generate-metadata can silently empty its lock file too — this
happened to `f/collection/baserow_webhook` and `f/data_platform/dbt_run`
while building this pipeline, and again when `generate-metadata` was
re-run by hand during unrelated troubleshooting. `windmill-push` now
snapshots every `*.script.lock` before calling `generate-metadata` and
restores any that come back with fewer pinned dependencies, logging a
warning when it does — so a normal `make windmill-push` self-heals this.
The risk is only if you call `wmill generate-metadata` directly (e.g. while
debugging) without going through `make windmill-push`/the installer: diff
the affected lock files against git afterward and `git checkout --` any
that got wiped before committing or pushing.

## 7. Validate end-to-end

Don't stop at "it deployed" — run it and check the data:

```sh
# trigger the extract job
curl -fsS -H "Host: windmill.localhost" -H "Authorization: Bearer $TOKEN" \
  -X POST "http://127.0.0.1/api/w/main/jobs/run/p/f/data_platform/extract_<name>" -d '{}'

# trigger dbt build — dbt_run resolves its own db resource via
# wmill.get_resource(), so no db arg here (only command/full_refresh, both optional)
curl -fsS -H "Host: windmill.localhost" -H "Authorization: Bearer $TOKEN" \
  -X POST "http://127.0.0.1/api/w/main/jobs/run/p/f/data_platform/dbt_run" -d '{}'

# poll http://127.0.0.1/api/w/main/jobs_u/get/<job_id> until type=CompletedJob

# confirm rows landed and look right
docker compose exec collection_db psql -U collection_admin -d collection \
  -c "select * from data_platform.mart_<name> limit 5;"
```

Then **run extraction a second time** and re-check the mart row count
against `count(distinct <natural_key>)` — if they diverge, the staging
dedupe in step 2 is missing or wrong.

## 8. If a script gets edited live instead of in the repo, audit before committing

This covers a script edited directly in the Windmill UI, or by an agent
driving the Windmill API/editor, instead of in this repo — not a
hypothetical: asking Hermes (or another agent) to "fix the pipeline" can
result in it editing the script directly in Windmill. `make windmill-pull`
brings that back, but don't commit a pull without diffing it first — two
things can silently regress, and Windmill's own tooling won't flag either
for you:

- **Comments and docstrings explaining *why*, not just *what*, get
  stripped.** A live edit (especially an LLM-driven one) tends to rewrite
  a docstring into something generically correct but stripped of the
  project-specific rationale (why DuckDB is ephemeral, why the project is
  mounted read-only, why `dbt build` not `dbt run`+`test`). Read the diff
  like a PR review, not a rubber stamp — re-add the rationale if it's
  gone; it's there so the *next* edit (human or agent) doesn't have to
  rediscover it.
- **The pulled `.script.yaml` can carry `schema: null`,** silently
  removing every parameter field from the script's UI "Run" form (the
  Python function's defaults still work, but nothing is overridable
  without hand-editing JSON args). This happens when a script gets
  deployed via a direct API/editor edit that skips Windmill's normal
  "infer schema from the function signature" step — it's not visible from
  the diff alone (`schema: null` doesn't look obviously wrong) and it
  silently persists through `wmill generate-metadata`: the staleness check
  is purely content-hash-based, and `make windmill-pull` already updated
  `wmill-lock.yaml`'s hash to match the *just-pulled* content, so
  generate-metadata sees "no change" and skips it even with
  `--schema-only`. To force a real regen:

  ```sh
  cd windmill
  # remove (or hand-edit) the affected path's line from wmill-lock.yaml first,
  # e.g. delete the "f/data_platform/dbt_run: <hash>" line
  wmill generate-metadata f/data_platform --schema-only --yes
  wmill generate-metadata rehash f/data_platform   # resync wmill-lock.yaml's hash after manual schema edits
  ```

  Then re-add any enum/description detail the auto-inference can't
  produce from a bare type+default (it only sees `command: str = "build"`,
  not that `build`/`run`/`test` are the only valid values) before pushing.
- **Re-run the dry-run push** (`wmill sync push --dry-run --yes
  --skip-branch-validation --show-diffs` from `windmill/`) after fixing
  either of the above, and confirm it shows only the files you actually
  touched before doing a real push.

## Things that will bite you if skipped

- Forgetting `meta.external_location` (using a bare `external:` key
  instead) → `Catalog Error: ... schema "datalake" does not exist`.
- No `schema: stg` (or equivalent) in `profiles.yml`'s `dev` target →
  dbt's startup schema-bootstrap resolves against the attached `pg`
  Postgres database, where the `data_platform` role has rights on its own
  schema but not database-level `CREATE` → `permission denied for
  database collection`.
- Relying on `dbt_project.yml`'s `+database:`/`+schema:` instead of an
  explicit `{{ config(...) }}` in the mart model → dbt-core's default
  `generate_schema_name` macro concatenates `<default_schema>_<custom_schema>`
  and your rows go to a schema you didn't expect (e.g. `stg_data_platform`).
- Running `dbt test` as a separate job after `dbt run` → staging-model
  tests fail with `Catalog Error: ... does not exist`, because staging
  lives in an ephemeral in-memory DuckDB scoped to a single job process.
  Use `dbt build` (run+test together) — it's already `dbt_run`'s default
  `command`.
- `/data_platform` is read-only — if your script needs dbt to write
  anything beyond `logs/`/`target/`, redirect it to `/tmp` the same way
  `dbt_run.py` does with `--target-path`/`--log-path`.

## Future work / not yet built

- **No flow definition** ties extract → dbt_run together; trigger them as
  two separate script runs (UI: Flows → chain manually, or two scheduled
  triggers) until a Windmill flow YAML is authored.
- **No per-pipeline dbt selection** — `dbt_run` always runs `build` against
  the whole project. Fine while there's one example pipeline; once there
  are several, consider a `select` parameter
  (`dbt build --select <model>+`) so one pipeline's schedule doesn't
  rebuild every other pipeline's marts too.
- **Hermes has no read access to `data_platform`.** This is deferred by
  design — see [docs/plans/datalake.md](plans/datalake.md) "Hermes — no
  wiring needed yet." Don't build MCP access as a side effect of adding a
  pipeline; that's a separate, explicit piece of work.
