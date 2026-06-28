"""
Runs the hn_stories dlt pipeline — path: f/data_platform/extract_hn_stories

Extracts Hacker News top stories and writes immutable Parquet under
/shared/datalake/hn_stories/<date>/. The dlt pipeline code itself lives in
the repo at data-platform/dlt/pipelines/hn_stories.py, bind-mounted
read-only into this worker at /data_platform (see docker-compose.yml
windmill_worker volumes) — kept in one place rather than duplicated into
this script.

See docs/plans/datalake.md for the overall architecture.
"""
import sys

sys.path.insert(0, "/data_platform/dlt/pipelines")


def main(limit: int = 20) -> dict:
    import os
    import uuid

    from hn_stories import run

    job_id = os.environ.get("WM_JOB_ID", str(uuid.uuid4()))
    return run(datalake_dir="/shared/datalake", limit=limit, job_id=job_id)
