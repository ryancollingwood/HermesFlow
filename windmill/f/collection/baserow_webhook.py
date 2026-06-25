"""
Receives Baserow row-webhook calls and persists the raw event into
collection.baserow_sync — path: f/collection/baserow_webhook

Baserow never gets direct SQL access into the `collection` schema (see
collection_db/initdb/01-init.sh and the functional-requirements gap analysis
for why) — this script is the sanctioned write path on Baserow's behalf.

Configure the webhook in Baserow's table settings (Webhooks tab) pointing at
this script's run-by-webhook URL, e.g.:
  http://windmill_server:8000/api/w/<workspace>/jobs/run/p/f/collection/baserow_webhook?token=<token>
Internal Docker DNS resolves this — both containers share the `agent`
network, so no Caddy/edge exposure is needed for this internal call.
"""
import json
from typing import TypedDict


class postgresql(TypedDict):
    host: str
    port: int
    user: str
    dbname: str
    password: str
    sslmode: str


def main(payload: dict, db: postgresql) -> dict:
    import psycopg2

    conn = psycopg2.connect(
        host=db["host"],
        port=db.get("port", 5432),
        dbname=db["dbname"],
        user=db["user"],
        password=db["password"],
        sslmode=db.get("sslmode", "disable"),
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS collection.baserow_sync (
                    id BIGSERIAL PRIMARY KEY,
                    table_id BIGINT,
                    event_type TEXT,
                    payload JSONB NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO collection.baserow_sync (table_id, event_type, payload)
                VALUES (%s, %s, %s)
                """,
                (
                    payload.get("table_id"),
                    payload.get("event_type", "unknown"),
                    json.dumps(payload),
                ),
            )
    finally:
        conn.close()
    return {"status": "ok"}
