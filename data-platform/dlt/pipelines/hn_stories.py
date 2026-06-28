"""
Example dlt source/pipeline for the data platform — extracts Hacker News
top story details and writes immutable Parquet to the datalake.

Run directly for local testing:
    python -m dlt pipeline ... (or just `python hn_stories.py`)

In Windmill, this module is invoked by
windmill/f/data_platform/extract_hn_stories.py — see docs/plans/datalake.md.

Every row carries the provenance columns described in datalake.md so any
row can be traced back to the run that produced it and the raw layer can be
rebuilt from scratch.
"""
import os
import uuid
from datetime import datetime, timezone

import dlt
import requests

HN_API = "https://hacker-news.firebaseio.com/v0"
PIPELINE_NAME = "hn_stories"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dlt.resource(name="stories", write_disposition="append")
def top_stories(limit: int = 20, job_id: str | None = None, batch_id: str | None = None):
    job_id = job_id or str(uuid.uuid4())
    batch_id = batch_id or str(uuid.uuid4())
    extracted_at = _now()

    ids = requests.get(f"{HN_API}/topstories.json", timeout=30).json()[:limit]
    for story_id in ids:
        url = f"{HN_API}/item/{story_id}.json"
        item = requests.get(url, timeout=30).json()
        if not item:
            continue
        yield {
            **item,
            "_extracted_at": extracted_at,
            "_job_id": job_id,
            "_pipeline": PIPELINE_NAME,
            "_batch_id": batch_id,
            "_source_url": url,
        }


def run(datalake_dir: str, limit: int = 20, job_id: str | None = None) -> dict:
    """Extracts and writes Parquet under <datalake_dir>/hn_stories/<date>/."""
    date_partition = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket_url = os.path.join(datalake_dir, PIPELINE_NAME, date_partition)

    pipeline = dlt.pipeline(
        pipeline_name=PIPELINE_NAME,
        destination=dlt.destinations.filesystem(bucket_url=bucket_url),
        dataset_name=PIPELINE_NAME,
    )
    load_info = pipeline.run(
        top_stories(limit=limit, job_id=job_id),
        loader_file_format="parquet",
    )
    return {
        "bucket_url": bucket_url,
        "load_info": str(load_info),
    }


if __name__ == "__main__":
    print(run(datalake_dir="/shared/datalake", limit=10))
