"""HF-027 bounded product-collection workflow integration and contract tests."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import yaml
from f.capabilities.collection.normalise_products import ProductNormalizationResult
from f.capabilities.collection.product_snapshot_write import persist_product_snapshots
from f.capabilities.collection.web_fetch import FetchAttempt, WebFetchResult
from f.libraries.lineage.helpers import enumerate_artifact_chain, write_artifact
from f.libraries.lineage.models import ArtifactStage
from f.libraries.results.models import ExecutionType, ResultOutcome
from f.libraries.storage.artifacts import FilesystemArtifactStore
from jsonschema import Draft202012Validator
from pydantic import ValidationError

ROOT = Path(__file__).parents[2]
FLOW_DIR = ROOT / "windmill" / "f" / "workflows" / "product_collection.flow"
FLOW_YAML = FLOW_DIR / "flow.yaml"
SCRIPT = FLOW_DIR / "run_product_collection.inline_script.py"
SCHEMA = ROOT / "docs" / "schemas" / "product_collection_workflow_result.schema.json"
FIXTURES = Path(__file__).parent / "fixtures" / "product_extraction"
MIGRATION_UP = ROOT / "collection_db" / "migrations" / "001_product_snapshots.up.sql"
MIGRATION_DOWN = ROOT / "collection_db" / "migrations" / "001_product_snapshots.down.sql"


def _load_workflow_module():
    spec = importlib.util.spec_from_file_location("hf027_product_collection", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workflow = _load_workflow_module()


def db_resource(dsn: str) -> dict:
    parsed = urlsplit(dsn)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
        "sslmode": "disable",
    }


def scalar(dsn: str, query: str):
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def product_collection_postgres():
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


class FixtureFetcher:
    """A bounded, artifact-retaining fetch test double with concurrency evidence."""

    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, url, allowed_domains, *, context, lineage, store, **_bounds):
        assert allowed_domains == ["fixture.example"]
        fixture_name = url.rsplit("/", 1)[-1]
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            if fixture_name in self.failures:
                raise RuntimeError(f"fixture source failed: {fixture_name}")
            content = (FIXTURES / fixture_name).read_bytes()
            artifact = write_artifact(
                lineage,
                store,
                context,
                content,
                stage=ArtifactStage.raw,
                media_type="text/html",
                metadata={"url": url, "fixture": fixture_name},
            )
            return WebFetchResult(
                status="success",
                requested_url=url,
                final_url=url,
                status_code=200,
                content_type="text/html",
                raw_artifact=artifact,
                attempts=[FetchAttempt(attempt=1, status="success", status_code=200)],
                lineage=lineage,
            )
        finally:
            with self.lock:
                self.active -= 1


def sources(names=("shopify.html", "nextjs.html", "opengraph.html")) -> list[dict]:
    return [
        {
            "source_id": f"fixture-{index}",
            "label": name.removesuffix(".html").title(),
            "url": f"https://fixture.example/{name}",
            "allowed_domains": ["fixture.example"],
        }
        for index, name in enumerate(names, start=1)
    ]


def run_fixture_workflow(dsn, tmp_path, *, names=None, failures=None, **kwargs):
    fetcher = FixtureFetcher(failures)
    result = workflow.run_product_collection(
        sources(names or ("shopify.html", "nextjs.html", "opengraph.html")),
        db_resource(dsn),
        job_id="job-hf027-fixtures",
        max_concurrency=2,
        store=FilesystemArtifactStore(tmp_path),
        fetcher=fetcher,
        **kwargs,
    )
    return result, fetcher


def test_three_fixture_sources_run_end_to_end_in_windmill(
    product_collection_postgres, tmp_path
):
    result, fetcher = run_fixture_workflow(product_collection_postgres, tmp_path)

    assert [source.status.value for source in result.sources] == [
        "success", "success", "success"
    ]
    assert all(source.raw_artifact and source.normalized_artifact for source in result.sources)
    assert sum(source.product_count for source in result.sources) == 3
    assert scalar(
        product_collection_postgres,
        "SELECT count(*) FROM collection.product_snapshots",
    ) == 3
    assert 2 <= fetcher.max_active <= result.max_concurrency
    assert result.execution_result.execution_type is ExecutionType.windmill_job
    assert result.execution_result.job.job_id == "job-hf027-fixtures"
    assert result.execution_result.workflow_path == workflow.WORKFLOW_PATH
    assert result.execution_result.capability_version == workflow.WORKFLOW_VERSION
    assert result.dataset_artifact and result.report_artifact
    assert len(result.execution_result.artifacts) == 8
    assert result.status in {"success", "partial"}
    assert result.execution_result.outcome in {ResultOutcome.success, ResultOutcome.partial}
    assert result.capability_versions == workflow.CAPABILITY_VERSIONS

    chain = enumerate_artifact_chain(result.lineage, [result.report_artifact.artifact_id])
    chain_ids = {artifact.artifact_id for artifact in chain}
    assert {source.raw_artifact.artifact_id for source in result.sources} <= chain_ids
    assert result.dataset_artifact.artifact_id in chain_ids
    assert chain[-1].artifact_id == result.report_artifact.artifact_id


def test_one_source_failure_is_isolated_and_successes_are_retained(
    product_collection_postgres, tmp_path
):
    result, _ = run_fixture_workflow(
        product_collection_postgres,
        tmp_path,
        failures={"nextjs.html"},
    )
    failed = [source for source in result.sources if source.status.value == "failed"]
    assert len(failed) == 1
    assert failed[0].source_id == "fixture-2"
    assert "fixture source failed" in failed[0].error
    assert all(
        source.raw_artifact and source.normalized_artifact
        for source in result.sources
        if source.status.value != "failed"
    )
    assert result.status == "partial"
    assert result.execution_result.outcome is ResultOutcome.partial
    assert result.report_artifact is not None
    assert scalar(
        product_collection_postgres,
        "SELECT count(*) FROM collection.product_snapshots",
    ) == 2


def test_total_source_failure_returns_failure_envelope_and_report(
    product_collection_postgres, tmp_path
):
    names = ("shopify.html", "nextjs.html", "opengraph.html")
    result, _ = run_fixture_workflow(
        product_collection_postgres,
        tmp_path,
        names=names,
        failures=set(names),
    )
    assert all(source.status.value == "failed" for source in result.sources)
    assert result.status == "failure"
    assert result.execution_result.outcome is ResultOutcome.failure
    assert result.execution_result.failure_summary == (
        "all product sources failed; review per-source errors"
    )
    assert result.dataset_artifact and result.report_artifact
    assert scalar(
        product_collection_postgres,
        "SELECT count(*) FROM collection.product_snapshots",
    ) == 0


def test_workflow_persistence_output_supports_idempotent_safe_retry(
    product_collection_postgres, tmp_path
):
    store = FilesystemArtifactStore(tmp_path)
    result, _ = run_fixture_workflow(
        product_collection_postgres,
        tmp_path,
        names=("nextjs.html",),
    )
    source = result.sources[0]
    normalization = ProductNormalizationResult.model_validate_json(
        store.read_text(source.normalized_artifact)
    )
    context = result.lineage.contexts[UUID(source.persistence_trace_id)]
    retry = persist_product_snapshots(
        normalization,
        context,
        db_resource(product_collection_postgres),
    )
    assert retry.inserted_count == retry.updated_count == 0
    assert retry.unchanged_count == source.product_count
    assert scalar(
        product_collection_postgres,
        "SELECT count(*) FROM collection.product_snapshots",
    ) == source.product_count


def test_ai_fallback_is_disabled_by_default_and_requires_explicit_resource(tmp_path):
    with pytest.raises(ValueError, match="Hermes connection is required"):
        workflow.run_product_collection(
            [{**sources(("nextjs.html",))[0], "enable_ai_fallback": True}],
            db_resource("postgresql://u:p@localhost/db"),
            job_id="job-ai-policy",
            store=FilesystemArtifactStore(tmp_path),
            fetcher=FixtureFetcher(),
        )

    seen = []

    def capture_extractor(*args, ai_conn=None, **kwargs):
        seen.append(ai_conn)
        return workflow.extract_products(*args, ai_conn=ai_conn, **kwargs)

    def preview_persist(normalization, context, _db):
        return persist_product_snapshots(normalization, context, preview=True)

    def no_database_comparison(_db, requests):
        return workflow.compare_from_database.__globals__["compare_snapshot_rows"]([], requests)

    workflow.run_product_collection(
        sources(("nextjs.html",)),
        db_resource("postgresql://u:p@localhost/db"),
        job_id="job-ai-default",
        store=FilesystemArtifactStore(tmp_path / "default"),
        fetcher=FixtureFetcher(),
        extractor=capture_extractor,
        persister=preview_persist,
        comparator=no_database_comparison,
    )
    assert seen == [None]


def test_input_bounds_and_duplicate_source_ids_are_rejected():
    one = sources(("nextjs.html",))[0]
    with pytest.raises(ValidationError):
        workflow.WorkflowInput(sources=[{**one, "source_id": str(i)} for i in range(21)])
    with pytest.raises(ValidationError):
        workflow.WorkflowInput(sources=[one], max_concurrency=9)
    with pytest.raises(ValidationError, match="duplicate source_id"):
        workflow.WorkflowInput(sources=[one, one])


def test_checked_in_contract_and_flow_definition_are_current():
    expected = workflow.ProductCollectionWorkflowResult.model_json_schema()
    Draft202012Validator.check_schema(expected)
    assert json.loads(SCHEMA.read_text()) == json.loads(json.dumps(expected, sort_keys=True))

    flow = yaml.safe_load(FLOW_YAML.read_text())
    modules = flow["value"]["modules"]
    assert len(modules) == 1
    assert modules[0]["value"]["type"] == "rawscript"
    assert modules[0]["value"]["content"] == "!inline run_product_collection.inline_script.py"
    assert modules[0]["value"]["lock"] == "!inline run_product_collection.inline_script.lock"
    source = SCRIPT.read_text()
    assert "WORKFLOW_PATH = \"f/workflows/product_collection\"" in source
    assert "subprocess" not in source
    assert "os.system" not in source
