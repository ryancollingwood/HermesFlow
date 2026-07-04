# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "dbt-core>=1.10",
#     "dbt-duckdb>=1.10",
#     "wmill>=1.0",
# ]
# ///

"""Runs the data_platform dbt project — path: f/data_platform/dbt_run

Staging models run against an in-memory DuckDB instance reading Parquet
from /shared/datalake (ephemeral per job — no shared .duckdb file, so no
locking contention across concurrent jobs). Mart models are materialized
through dbt-duckdb's Postgres attach feature directly into collection_db's
`data_platform` schema — see data-platform/dbt/profiles.yml and
docs/plans/datalake.md.

The dbt project itself lives in the repo at data-platform/dbt/, bind-mounted
read-only into this worker at /data_platform (see docker-compose.yml
windmill_worker volumes).

The db resource is fetched directly via wmill.get_resource() rather than
taken as an injected parameter, so a run can never reach this far with a
missing/empty resource — it fails fast inside get_resource() instead.
"""
import os


def main(command: str = "build", full_refresh: bool = False) -> dict:
    from wmill import get_resource
    from dbt.cli.main import dbtRunner

    db = get_resource("f/data_platform/data_platform_db")

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
