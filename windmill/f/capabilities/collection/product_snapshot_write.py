"""HF-025 idempotent persistence for normalized product snapshots."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Callable, Optional, TypedDict
from uuid import UUID

import psycopg2
from pydantic import BaseModel, ConfigDict, Field

from f.capabilities.collection.normalise_products import ProductNormalizationResult
from f.libraries.lineage.models import ExecutionContext

CAPABILITY_PATH = "f/capabilities/collection/product_snapshot_write"
CAPABILITY_VERSION = "1.0.0"


class postgresql(TypedDict):
    host: str
    port: int
    user: str
    dbname: str
    password: str
    sslmode: str


class SnapshotDisposition(str, Enum):
    planned = "planned"
    inserted = "inserted"
    updated = "updated"
    unchanged = "unchanged"


class SnapshotWriteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: Optional[int] = Field(default=None, ge=1)
    normalized_product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    payload_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    disposition: SnapshotDisposition


class ProductSnapshotWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    status: str
    preview: bool
    execution_trace_id: UUID
    source_trace_id: UUID
    source_artifact_id: UUID
    planned_count: int = Field(..., ge=0)
    inserted_count: int = Field(..., ge=0)
    updated_count: int = Field(..., ge=0)
    unchanged_count: int = Field(..., ge=0)
    records: list[SnapshotWriteRecord]


_UPSERT = """
WITH upserted AS (
INSERT INTO collection.product_snapshots (
    execution_trace_id,
    source_trace_id,
    source_artifact_id,
    source_content_hash,
    normalized_product_id,
    source_product_id,
    schema_version,
    normalization_version,
    product_payload,
    payload_hash
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (
    execution_trace_id,
    source_artifact_id,
    normalized_product_id
) DO UPDATE SET
    source_trace_id = EXCLUDED.source_trace_id,
    source_content_hash = EXCLUDED.source_content_hash,
    source_product_id = EXCLUDED.source_product_id,
    schema_version = EXCLUDED.schema_version,
    normalization_version = EXCLUDED.normalization_version,
    product_payload = EXCLUDED.product_payload,
    payload_hash = EXCLUDED.payload_hash,
    updated_at = now()
WHERE collection.product_snapshots.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
RETURNING snapshot_id, (xmax = 0) AS inserted
)
SELECT snapshot_id, inserted, false AS unchanged FROM upserted
UNION ALL
SELECT snapshot_id, false AS inserted, true AS unchanged
FROM collection.product_snapshots
WHERE execution_trace_id = %s
  AND source_artifact_id = %s
  AND normalized_product_id = %s
  AND NOT EXISTS (SELECT 1 FROM upserted)
"""


def _canonical_payload(product) -> tuple[str, str]:
    payload = json.dumps(
        product.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return payload, hashlib.sha256(payload.encode()).hexdigest()


def _connect(db: postgresql):
    return psycopg2.connect(
        host=db["host"],
        port=db.get("port", 5432),
        dbname=db["dbname"],
        user=db["user"],
        password=db["password"],
        sslmode=db.get("sslmode", "disable"),
    )


def persist_product_snapshots(
    product_normalization: ProductNormalizationResult | dict,
    execution_context: ExecutionContext | dict,
    db: Optional[postgresql] = None,
    *,
    preview: bool = False,
    connection_factory: Optional[Callable] = None,
) -> ProductSnapshotWriteResult:
    """Plan or transactionally upsert every normalized product snapshot."""
    normalization = ProductNormalizationResult.model_validate(product_normalization)
    execution = ExecutionContext.model_validate(execution_context)
    source = normalization.source_artifact
    plans = []
    payloads = []
    for product in normalization.products:
        payload, payload_hash = _canonical_payload(product)
        payloads.append((product, payload, payload_hash))
        plans.append(SnapshotWriteRecord(
            normalized_product_id=product.normalized_product_id,
            source_product_id=product.source_product_id,
            payload_hash=payload_hash,
            disposition=SnapshotDisposition.planned,
        ))
    if preview:
        return ProductSnapshotWriteResult(
            status="preview",
            preview=True,
            execution_trace_id=execution.trace_id,
            source_trace_id=source.trace_id,
            source_artifact_id=source.artifact_id,
            planned_count=len(plans),
            inserted_count=0,
            updated_count=0,
            unchanged_count=0,
            records=plans,
        )
    if db is None:
        raise ValueError("db is required when preview is false")
    connect = connection_factory or _connect
    conn = connect(db)
    records = []
    try:
        with conn:
            with conn.cursor() as cursor:
                for product, payload, payload_hash in payloads:
                    cursor.execute(_UPSERT, (
                        str(execution.trace_id),
                        str(source.trace_id),
                        str(source.artifact_id),
                        source.content_hash,
                        product.normalized_product_id,
                        product.source_product_id,
                        normalization.schema_version,
                        normalization.normalization_version,
                        payload,
                        payload_hash,
                        str(execution.trace_id),
                        str(source.artifact_id),
                        product.normalized_product_id,
                    ))
                    snapshot_id, inserted, unchanged = cursor.fetchone()
                    records.append(SnapshotWriteRecord(
                        snapshot_id=snapshot_id,
                        normalized_product_id=product.normalized_product_id,
                        source_product_id=product.source_product_id,
                        payload_hash=payload_hash,
                        disposition=(SnapshotDisposition.unchanged if unchanged else (
                            SnapshotDisposition.inserted
                            if inserted else SnapshotDisposition.updated
                        )),
                    ))
    finally:
        conn.close()
    inserted_count = sum(
        record.disposition == SnapshotDisposition.inserted for record in records
    )
    unchanged_count = sum(
        record.disposition == SnapshotDisposition.unchanged for record in records
    )
    return ProductSnapshotWriteResult(
        status="persisted",
        preview=False,
        execution_trace_id=execution.trace_id,
        source_trace_id=source.trace_id,
        source_artifact_id=source.artifact_id,
        planned_count=len(plans),
        inserted_count=inserted_count,
        updated_count=len(records) - inserted_count - unchanged_count,
        unchanged_count=unchanged_count,
        records=records,
    )


def main(
    product_normalization: dict,
    execution_context: dict,
    db: postgresql,
    preview: bool = False,
) -> dict:
    return persist_product_snapshots(
        product_normalization,
        execution_context,
        db,
        preview=preview,
    ).model_dump(mode="json")
