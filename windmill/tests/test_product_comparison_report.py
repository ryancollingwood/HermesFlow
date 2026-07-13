"""HF-026 deterministic comparison, golden report, and artifact-lineage tests."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from f.capabilities.collection.compare_product_snapshots import (
    ProductComparisonResult,
    SnapshotRow,
    compare_from_database,
    compare_snapshot_rows,
)
from f.capabilities.collection.extract_products import ProductProvenance
from f.capabilities.collection.normalise_products import (
    Availability,
    AvailabilityStatus,
    CurrencyStatus,
    NormalizedIdentifiers,
    NormalizedOffer,
    NormalizedProduct,
    OriginalOffer,
    ValueStatus,
)
from f.capabilities.collection.render_product_report import (
    ProductReportResult,
    render_product_report,
    store_product_report,
)
from f.libraries.lineage.helpers import (
    begin_lineage,
    child_context,
    enumerate_artifact_chain,
    write_artifact,
)
from f.libraries.lineage.models import ArtifactStage
from f.libraries.storage.artifacts import FilesystemArtifactStore


FIXTURES = Path(__file__).parent / "fixtures" / "product_comparison"
SCHEMAS = Path(__file__).parents[2] / "docs" / "schemas"
MIGRATION_UP = Path(__file__).parents[2] / "collection_db" / "migrations" / "001_product_snapshots.up.sql"
MIGRATION_DOWN = Path(__file__).parents[2] / "collection_db" / "migrations" / "001_product_snapshots.down.sql"
EXEC_A = UUID("10000000-0000-0000-0000-000000000001")
EXEC_B = UUID("10000000-0000-0000-0000-000000000002")
EXEC_EMPTY = UUID("10000000-0000-0000-0000-000000000003")
TRACE_A = UUID("20000000-0000-0000-0000-000000000001")
TRACE_B = UUID("20000000-0000-0000-0000-000000000002")
ARTIFACT_A = UUID("30000000-0000-0000-0000-000000000001")
ARTIFACT_B = UUID("30000000-0000-0000-0000-000000000002")


def product(number, name, artifact_id, *, gtin=None, price=None, currency="AUD"):
    normalized_id = f"{number + 100:064x}"
    source_id = f"{number:064x}"
    offers = []
    if price is not None:
        offers.append(NormalizedOffer(
            amount=price,
            price_status=ValueStatus.valid,
            currency=currency,
            currency_status=CurrencyStatus.valid,
            availability=Availability.in_stock,
            availability_status=AvailabilityStatus.recognized,
            source_offer_index=0,
            original=OriginalOffer(price=price, currency=currency),
        ))
    return NormalizedProduct(
        normalized_product_id=normalized_id,
        source_product_id=source_id,
        name=name,
        brand="Example",
        identifiers=NormalizedIdentifiers(
            sku=f"SKU-{number}",
            gtin=gtin,
            missing=[field for field, value in (("gtin", gtin), ("mpn", None)) if value is None],
        ),
        offers=offers,
        provenance=ProductProvenance(
            extraction_method="structured_markup",
            source_artifact_id=str(artifact_id),
            source_content_hash="a" * 64,
            evidence_paths=["jsonld[0]"],
        ),
        original={"name": name},
    )


def row(snapshot_id, execution_id, trace_id, artifact_id, item):
    return SnapshotRow(
        snapshot_id=snapshot_id,
        execution_trace_id=execution_id,
        source_trace_id=trace_id,
        source_artifact_id=artifact_id,
        source_content_hash="a" * 64,
        normalized_product_id=item.normalized_product_id,
        source_product_id=item.source_product_id,
        product_payload=item,
    )


def representative_inputs():
    rows = [
        row(1, EXEC_A, TRACE_A, ARTIFACT_A, product(
            1, "Canvas Tote", ARTIFACT_A, gtin="123456789012", price="10"
        )),
        row(2, EXEC_A, TRACE_A, ARTIFACT_A, product(
            2, "Canvas Tote duplicate", ARTIFACT_A, gtin="123456789012", price="11"
        )),
        row(3, EXEC_A, TRACE_A, ARTIFACT_A, product(
            3, "Desk Lamp", ARTIFACT_A
        )),
        row(4, EXEC_B, TRACE_B, ARTIFACT_B, product(
            4, "Canvas Tote", ARTIFACT_B, gtin="123456789012", price="12.5"
        )),
    ]
    requests = [
        {"execution_trace_id": str(EXEC_A), "label": "Store A"},
        {"execution_trace_id": str(EXEC_B), "label": "Store B"},
        {"execution_trace_id": str(EXEC_EMPTY), "label": "Empty store"},
    ]
    return rows, requests


def representative_comparison():
    rows, requests = representative_inputs()
    return compare_snapshot_rows(rows, requests)


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
def comparison_postgres():
    dsn = os.environ.get("HF_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set HF_TEST_POSTGRES_DSN to a disposable PostgreSQL instance")
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS collection CASCADE")
        cursor.execute("CREATE SCHEMA collection")
        cursor.execute(MIGRATION_UP.read_text())
    conn.close()
    try:
        yield dsn
    finally:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(MIGRATION_DOWN.read_text())
            cursor.execute("DROP SCHEMA IF EXISTS collection CASCADE")
        conn.close()


def test_comparison_matches_golden_dataset():
    actual = representative_comparison().model_dump(mode="json")
    assert actual == json.loads((FIXTURES / "comparison.json").read_text())


def test_report_matches_golden_markdown():
    assert render_product_report(representative_comparison()) == (
        FIXTURES / "report.md"
    ).read_text()


def test_empty_source_missing_price_and_duplicate_are_explicit():
    result = representative_comparison()
    assert result.summary.empty_source_count == 1
    assert result.summary.duplicate_product_count == 1
    assert result.summary.comparable_price_count == 1
    assert [warning.code for warning in result.warnings] == [
        "duplicate_product",
        "empty_source",
        "missing_comparable_price",
    ]
    matched = next(group for group in result.products if group.match_key.startswith("gtin:"))
    assert matched.price_differences[0].absolute_difference == "2.5"
    assert matched.price_differences[0].percentage_difference == "25"


def test_only_empty_source_returns_valid_empty_dataset():
    result = compare_snapshot_rows([], [{
        "execution_trace_id": str(EXEC_EMPTY), "label": "Empty store"
    }])
    assert result.products == []
    assert result.summary.snapshot_count == 0
    assert result.summary.warning_count == 1
    assert result.warnings[0].code == "empty_source"


def test_comparison_is_deterministic_under_input_reordering():
    rows, requests = representative_inputs()
    forward = compare_snapshot_rows(rows, requests).model_dump(mode="json")
    reversed_input = compare_snapshot_rows(
        list(reversed(rows)), list(reversed(requests))
    ).model_dump(mode="json")
    assert forward == reversed_input


def test_database_loader_reads_requested_persisted_snapshots(comparison_postgres):
    import psycopg2

    rows, requests = representative_inputs()
    selected = [rows[0], rows[3]]
    conn = psycopg2.connect(comparison_postgres)
    with conn, conn.cursor() as cursor:
        for item in selected:
            payload = json.dumps(item.product_payload.model_dump(mode="json"))
            cursor.execute("""
                INSERT INTO collection.product_snapshots (
                    execution_trace_id, source_trace_id, source_artifact_id,
                    source_content_hash, normalized_product_id, source_product_id,
                    schema_version, normalization_version, product_payload, payload_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, '1.0', '1.0.0', %s::jsonb, %s)
            """, (
                str(item.execution_trace_id), str(item.source_trace_id),
                str(item.source_artifact_id), item.source_content_hash,
                item.normalized_product_id, item.source_product_id, payload, "f" * 64,
            ))
    conn.close()
    result = compare_from_database(db_resource(comparison_postgres), requests[:2])
    assert result.summary.snapshot_count == 2
    assert result.summary.unique_product_count == 1
    assert result.products[0].price_differences[0].absolute_difference == "2.5"


def test_report_artifacts_and_envelope_preserve_raw_lineage(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    state, root = begin_lineage(
        capability="f/workflows/product_collection",
        capability_version="1.0.0",
        initiating_actor="test",
    )
    source_a_context = child_context(
        state, root,
        capability="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
    )
    source_b_context = child_context(
        state, root,
        capability="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
    )
    report_context = child_context(
        state, root,
        capability="f/capabilities/collection/render_product_report",
        capability_version="1.0.0",
    )
    source_a = write_artifact(
        state, store, source_a_context, "source A",
        stage=ArtifactStage.raw, media_type="text/html",
    )
    source_b = write_artifact(
        state, store, source_b_context, "source B",
        stage=ArtifactStage.raw, media_type="text/html",
    )
    rows = [
        row(1, source_a_context.trace_id, source_a.trace_id, source_a.artifact_id,
            product(1, "Canvas Tote", source_a.artifact_id,
                    gtin="123456789012", price="10")),
        row(2, source_b_context.trace_id, source_b.trace_id, source_b.artifact_id,
            product(2, "Canvas Tote", source_b.artifact_id,
                    gtin="123456789012", price="12")),
    ]
    for item, artifact in ((rows[0], source_a), (rows[1], source_b)):
        item.source_content_hash = artifact.content_hash
        item.product_payload.provenance.source_content_hash = artifact.content_hash
    comparison = compare_snapshot_rows(rows, [
        {"execution_trace_id": str(source_a_context.trace_id), "label": "A"},
        {"execution_trace_id": str(source_b_context.trace_id), "label": "B"},
    ])
    result = store_product_report(
        comparison,
        state,
        report_context,
        {"job_id": "job-hf026", "workspace": "main", "path": "f/workflows/product_collection"},
        store=store,
        duration_seconds=1.25,
    )
    chain = enumerate_artifact_chain(result.lineage, [result.report_artifact.artifact_id])
    assert {artifact.artifact_id for artifact in chain[:2]} == {
        source_a.artifact_id, source_b.artifact_id
    }
    assert [artifact.artifact_id for artifact in chain[2:]] == [
        result.dataset_artifact.artifact_id,
        result.report_artifact.artifact_id,
    ]
    assert result.execution_result.job.job_id == "job-hf026"
    assert [item.artifact_id for item in result.execution_result.artifacts] == [
        result.dataset_artifact.artifact_id,
        result.report_artifact.artifact_id,
    ]
    assert result.execution_result.outcome.value == "success"
    assert store.read_text(result.report_artifact) == result.report_text
    assert json.loads(store.read_text(result.dataset_artifact))["summary"] == (
        comparison.summary.model_dump(mode="json")
    )


def test_report_rejects_missing_source_lineage(tmp_path):
    state, context = begin_lineage(
        capability="f/capabilities/collection/render_product_report",
        capability_version="1.0.0",
        initiating_actor="test",
    )
    with pytest.raises(ValueError, match="absent from lineage"):
        store_product_report(
            representative_comparison(),
            state,
            context,
            {"job_id": "job-hf026", "path": "f/workflows/product_collection"},
            store=FilesystemArtifactStore(tmp_path),
        )


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (ProductComparisonResult, "product_comparison_result.schema.json"),
        (ProductReportResult, "product_report_result.schema.json"),
    ],
)
def test_contract_schema_matches_checked_in_copy(model, filename):
    expected = model.model_json_schema()
    Draft202012Validator.check_schema(expected)
    assert json.loads((SCHEMAS / filename).read_text()) == json.loads(
        json.dumps(expected, sort_keys=True)
    )
