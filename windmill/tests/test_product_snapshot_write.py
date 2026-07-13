"""HF-025 snapshot persistence unit and disposable-Postgres integration tests."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from f.capabilities.collection.extract_products import ProductProvenance
from f.capabilities.collection.normalise_products import (
    NormalizedIdentifiers,
    NormalizedProduct,
    ProductNormalizationResult,
)
from f.capabilities.collection.product_snapshot_write import (
    ProductSnapshotWriteResult,
    persist_product_snapshots,
)
from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext


ROOT = Path(__file__).parents[2]
UP = ROOT / "collection_db" / "migrations" / "001_product_snapshots.up.sql"
DOWN = ROOT / "collection_db" / "migrations" / "001_product_snapshots.down.sql"
SCHEMA = ROOT / "docs" / "schemas" / "product_snapshot_write_result.schema.json"
SOURCE_TRACE_ID = UUID("10000000-0000-0000-0000-000000000001")
SOURCE_ARTIFACT_ID = UUID("20000000-0000-0000-0000-000000000001")
EXECUTION_TRACE_ID = UUID("30000000-0000-0000-0000-000000000001")


def normalization(*, names=("Canvas Tote",)):
    artifact = ArtifactRef(
        artifact_id=SOURCE_ARTIFACT_ID,
        trace_id=SOURCE_TRACE_ID,
        stage=ArtifactStage.raw,
        content_hash="a" * 64,
        storage_uri="file:///fixtures/source.html",
        creator_capability="f/capabilities/collection/web_fetch",
        creator_capability_version="1.0.0",
    )
    products = []
    for index, name in enumerate(names, start=1):
        source_product_id = f"{index:064x}"
        products.append(NormalizedProduct(
            normalized_product_id=f"{index + 100:064x}",
            source_product_id=source_product_id,
            name=name,
            identifiers=NormalizedIdentifiers(
                sku=f"SKU-{index}", missing=["gtin", "mpn"]
            ),
            provenance=ProductProvenance(
                extraction_method="structured_markup",
                source_artifact_id=str(SOURCE_ARTIFACT_ID),
                source_content_hash="a" * 64,
                evidence_paths=[f"jsonld[0].products[{index - 1}]"],
            ),
            original={"product_id": source_product_id, "name": name},
        ))
    return ProductNormalizationResult(
        status="success",
        source_schema_version="1.0",
        source_artifact=artifact,
        products=products,
    )


def execution(trace_id=EXECUTION_TRACE_ID):
    return ExecutionContext(
        trace_id=trace_id,
        capability="f/capabilities/collection/product_snapshot_write",
        capability_version="1.0.0",
        initiating_actor="test",
    )


def db_resource(dsn):
    parsed = urlsplit(dsn)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
        "sslmode": "disable",
    }


@pytest.fixture
def migrated_postgres():
    dsn = os.environ.get("HF_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set HF_TEST_POSTGRES_DSN to a disposable PostgreSQL instance")
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS collection CASCADE")
        cursor.execute("CREATE SCHEMA collection")
        cursor.execute(UP.read_text())
    conn.close()
    try:
        yield dsn
    finally:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(DOWN.read_text())
            cursor.execute("DROP SCHEMA IF EXISTS collection CASCADE")
        conn.close()


def scalar(dsn, query):
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchone()[0]
    finally:
        conn.close()


def test_preview_is_read_only_and_does_not_connect():
    def forbidden(_db):
        raise AssertionError("preview must not open a database connection")

    result = persist_product_snapshots(
        normalization(),
        execution(),
        preview=True,
        connection_factory=forbidden,
    )
    assert result.status == "preview"
    assert result.planned_count == 1
    assert result.inserted_count == result.updated_count == result.unchanged_count == 0
    assert result.records[0].snapshot_id is None
    assert result.records[0].disposition.value == "planned"


def test_contract_schema_matches_checked_in_copy():
    expected = ProductSnapshotWriteResult.model_json_schema()
    Draft202012Validator.check_schema(expected)
    assert json.loads(SCHEMA.read_text()) == json.loads(
        json.dumps(expected, sort_keys=True)
    )


def test_migration_applies_and_reverses(migrated_postgres):
    assert scalar(
        migrated_postgres,
        "SELECT to_regclass('collection.product_snapshots')::text",
    ) == "collection.product_snapshots"
    assert scalar(
        migrated_postgres,
        "SELECT count(*) FROM collection.hermesflow_schema_migrations "
        "WHERE version = '001_product_snapshots'",
    ) == 1

    import psycopg2

    conn = psycopg2.connect(migrated_postgres)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(DOWN.read_text())
    conn.close()
    assert scalar(
        migrated_postgres,
        "SELECT to_regclass('collection.product_snapshots') IS NULL",
    ) is True


def test_rerun_upserts_one_execution_source_product_row(migrated_postgres):
    db = db_resource(migrated_postgres)
    first = persist_product_snapshots(normalization(), execution(), db)
    second = persist_product_snapshots(normalization(), execution(), db)
    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.updated_count == 0
    assert second.unchanged_count == 1
    assert first.records[0].snapshot_id == second.records[0].snapshot_id
    assert scalar(
        migrated_postgres, "SELECT count(*) FROM collection.product_snapshots"
    ) == 1

    changed = normalization()
    changed.products[0].name = "Updated Canvas Tote"
    updated = persist_product_snapshots(changed, execution(), db)
    assert updated.updated_count == 1
    assert updated.unchanged_count == 0
    assert scalar(
        migrated_postgres,
        "SELECT product_payload->>'name' FROM collection.product_snapshots",
    ) == "Updated Canvas Tote"


def test_execution_is_part_of_the_idempotency_key(migrated_postgres):
    db = db_resource(migrated_postgres)
    persist_product_snapshots(normalization(), execution(), db)
    persist_product_snapshots(
        normalization(),
        execution(UUID("30000000-0000-0000-0000-000000000002")),
        db,
    )
    assert scalar(
        migrated_postgres, "SELECT count(*) FROM collection.product_snapshots"
    ) == 2


def test_source_artifact_is_part_of_the_idempotency_key(migrated_postgres):
    db = db_resource(migrated_postgres)
    first = normalization()
    second = normalization()
    second.source_artifact.artifact_id = UUID(
        "20000000-0000-0000-0000-000000000002"
    )
    persist_product_snapshots(first, execution(), db)
    persist_product_snapshots(second, execution(), db)
    assert scalar(
        migrated_postgres, "SELECT count(*) FROM collection.product_snapshots"
    ) == 2


def test_partial_batch_failure_rolls_back_every_row(migrated_postgres):
    import psycopg2

    conn = psycopg2.connect(migrated_postgres)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION collection.reject_failed_snapshot() RETURNS trigger AS $$
            BEGIN
                IF NEW.product_payload->>'name' = 'FAIL' THEN
                    RAISE EXCEPTION 'forced snapshot failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        cursor.execute("""
            CREATE TRIGGER reject_failed_snapshot
            BEFORE INSERT OR UPDATE ON collection.product_snapshots
            FOR EACH ROW EXECUTE FUNCTION collection.reject_failed_snapshot()
        """)
    conn.close()

    with pytest.raises(psycopg2.Error, match="forced snapshot failure"):
        persist_product_snapshots(
            normalization(names=("Good", "FAIL")),
            execution(),
            db_resource(migrated_postgres),
        )
    assert scalar(
        migrated_postgres, "SELECT count(*) FROM collection.product_snapshots"
    ) == 0
