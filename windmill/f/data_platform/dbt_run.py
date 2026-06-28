"""
Runs the data_platform dbt project — path: f/data_platform/dbt_run

Staging models run against an in-memory DuckDB instance reading Parquet
from /shared/datalake (ephemeral per job — no shared .duckdb file, so no
locking contention across concurrent jobs). Mart models are materialized
through dbt-duckdb's Postgres attach feature directly into collection_db's
`data_platform` schema — see data-platform/dbt/profiles.yml and
docs/plans/datalake.md.

The dbt project itself lives in the repo at data-platform/dbt/, bind-mounted
read-only into this worker at /data_platform (see docker-compose.yml
windmill_worker volumes).

`db` is the f/data_platform/data_platform_db Postgres resource — its fields
are exported as the env vars data-platform/dbt/profiles.yml's `attach`
block expects (env_var(...)), so the resource (and its $var: secret) stays
the single source of truth instead of duplicating credentials into a second
dbt-specific config.
"""
import os
from typing import TypedDict


class postgresql(TypedDict):
    host: str
    port: int
    user: str
    dbname: str
    password: str
    sslmode: str


def main(db: postgresql, command: str = "build", full_refresh: bool = False) -> dict:
    # Default is `build` (run+test in one process), not `run` — staging
    # models live in an ephemeral in-memory DuckDB scoped to this job, so a
    # separate `test` job in a later run has nothing to check against. Only
    # `build`/`run` validate staging in the same process that created it.
    from dbt.cli.main import dbtRunner

    os.environ["COLLECTION_DB_HOST"] = db["host"]
    os.environ["COLLECTION_DB_PORT_INTERNAL"] = str(db.get("port", 5432))
    os.environ["COLLECTION_DB_NAME"] = db["dbname"]
    os.environ["DATA_PLATFORM_DB_USER"] = db["user"]
    os.environ["DATA_PLATFORM_DB_PASSWORD"] = db["password"]

    args = [
        command,
        "--project-dir", "/data_platform/dbt",
        "--profiles-dir", "/data_platform/dbt",
        # /data_platform is mounted read-only — dbt's own scratch dirs need
        # somewhere writable, ephemeral per job is fine.
        "--target-path", "/tmp/dbt_target",
        "--log-path", "/tmp/dbt_logs",
    ]
    if command == "run" and full_refresh:
        args.append("--full-refresh")

    res = dbtRunner().invoke(args)
    if not res.success:
        raise RuntimeError(f"dbt {command} failed: {res.exception}")
    return {"success": res.success, "command": command}
