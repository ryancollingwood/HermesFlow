"""HF-029 bounded failure-inspection and classification tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from f.hermes_flow.catalogue.models import CapabilityKind, Catalogue, CatalogueEntry
from f.hermes_flow.repair.inspection import (
    FailureInspectionError,
    build_repair_context,
    classify_failure,
    inspect_failure_from_windmill,
)
from f.hermes_flow.repair.models import (
    FailureCategory,
    RepairContext,
    RepairContextLimits,
)
from f.libraries.capability.models import CapabilityMaturity, CapabilityMetadata

CAPABILITY = "f/capabilities/collection/extract_products"
CONSUMER = "f/workflows/product_collection"
SCHEMA = Path(__file__).parents[2] / "docs" / "schemas" / "repair_context.schema.json"


def entry(path, *, dependencies=(), tests=(), kind=CapabilityKind.script):
    return CatalogueEntry(
        kind=kind,
        tags=[],
        inputs_summary="input",
        outputs_summary="output",
        metadata=CapabilityMetadata(
            path=path,
            capability_version="1.2.3",
            summary=path,
            maturity=CapabilityMaturity.experimental,
            owners=["platform"],
            dependencies=list(dependencies),
            test_requirements=list(tests),
        ),
    )


CATALOGUE = Catalogue(entries=[
    entry(CAPABILITY, tests=["contracts/extract"]),
    entry(CONSUMER, dependencies=[CAPABILITY], tests=["smoke/workflow"], kind=CapabilityKind.flow),
])


def catalogue_yaml():
    return json.dumps(CATALOGUE.model_dump(mode="json"))


def failed_job(**overrides):
    job = {
        "id": "019f6000-0000-0000-0000-000000000001",
        "script_path": CAPABILITY,
        "success": False,
        "args": {"source_url": "https://shop.example/item"},
        "error": {"name": "ParserError", "message": "selector not found after markup changed"},
        "result": {
            "artifacts": [{
                "artifact_id": "11111111-1111-1111-1111-111111111111",
                "stage": "raw",
                "storage_uri": "file:///shared/artifacts/aa/example",
                "description": "failed source",
            }]
        },
    }
    job.update(overrides)
    return job


@pytest.mark.parametrize(("evidence", "expected"), [
    ("ValidationError: missing required field url", FailureCategory.input),
    ("Parser failed: selector not found after markup changed", FailureCategory.source_drift),
    ("Traceback: NameError: parser_factory is not defined", FailureCategory.code_defect),
    ("ModuleNotFoundError: No module named lxml", FailureCategory.dependency),
    ("403 Forbidden: blocked by policy", FailureCategory.policy),
    ("ConnectError: endpoint unavailable", FailureCategory.infrastructure),
    ("something novel happened", FailureCategory.unknown),
])
def test_failure_classification_examples(evidence, expected):
    classification = classify_failure(evidence)
    assert classification.category is expected
    assert classification.reasons


def test_complete_context_links_job_and_gathers_each_evidence_category():
    context = build_repair_context(
        job=failed_job(),
        active_asset={
            "hash": "active-windmill-hash",
            "content": "def parse(page):\n    return page.select('.product')\n",
        },
        catalogue=CATALOGUE,
        logs="Parser failed because selector not found",
        recent_test_evidence=[
            {
                "test": "contracts/extract", "capability_paths": [CAPABILITY],
                "status": "failed", "job_id": "test-job", "recorded_at": "2026-07-14T01:00:00Z",
            },
            {
                "test": "unrelated", "capability_paths": ["f/other"], "status": "passed",
            },
        ],
        workspace="main",
        windmill_base_url="https://windmill.example",
    )

    assert context.original_job.job_id == failed_job()["id"]
    assert context.original_job.api_url.endswith(f"/api/w/main/jobs_u/get/{failed_job()['id']}")
    assert context.active_capability.capability_version == "1.2.3"
    assert context.active_capability.windmill_hash == "active-windmill-hash"
    assert "source_url" in context.inputs.content
    assert "selector not found" in context.logs.content
    assert context.artifacts[0].artifact_id == "11111111-1111-1111-1111-111111111111"
    assert context.dependency_impact[0].path == CONSUMER
    assert context.dependency_impact[0].tests == ["smoke/workflow"]
    assert [test.test for test in context.recent_test_evidence] == ["contracts/extract"]
    assert context.classification.category is FailureCategory.source_drift
    assert context.total_bytes == context.serialized_size()


def test_actual_windmill_job_kind_selects_flow_asset():
    context = build_repair_context(
        job=failed_job(
            script_path=CONSUMER,
            job_kind="flow",
            error="ValidationError: missing required field sources",
        ),
        active_asset={"value": {"modules": []}},
        catalogue=CATALOGUE,
    )
    assert context.active_capability.asset_kind == "flow"


def test_context_is_bounded_and_records_every_omission():
    artifacts = [
        {"artifact_id": f"artifact-{index}", "storage_uri": f"file:///artifact/{index}"}
        for index in range(12)
    ]
    tests = [{
        "test": f"test-{index}", "capability_paths": [CAPABILITY], "status": "failed",
        "details": "x" * 1000,
    } for index in range(12)]
    limits = RepairContextLimits(
        max_total_bytes=16_384,
        max_code_bytes=8_000,
        max_input_bytes=4_000,
        max_log_bytes=8_000,
        max_artifacts=3,
        max_dependencies=1,
        max_test_evidence=3,
    )
    context = build_repair_context(
        job=failed_job(
            args={"payload": "i" * 20_000},
            result={"artifacts": artifacts},
            error="Traceback: NameError " + "e" * 5000,
        ),
        active_asset={"content": "c" * 40_000},
        catalogue=CATALOGUE,
        logs="l" * 40_000,
        recent_test_evidence=tests,
        limits=limits,
    )

    assert context.serialized_size() <= limits.max_total_bytes
    assert set(context.truncation.truncated_sections) >= {
        "active_capability.code", "inputs", "logs"
    }
    assert context.truncation.omitted_artifacts == 9
    assert context.truncation.omitted_test_evidence >= 9
    assert context.active_capability.code.sha256
    assert context.logs.original_bytes == 40_000


def test_secrets_are_redacted_and_conversation_data_is_excluded_everywhere():
    secrets = [
        "super-secret-password", "github-token-value-123",
        "openai-token-value-456", "database-password",
    ]
    context = build_repair_context(
        job=failed_job(
            args={
                "password": secrets[0],
                "nested": {"api_key": secrets[2]},
                "input_artifact": {
                    "artifact_id": "input-artifact",
                    "storage_uri": f"https://artifacts.example/item?token={secrets[1]}",
                },
                "conversation": [{"role": "user", "content": "unrelated-private-chat"}],
                "messages": ["another-private-message"],
            },
            error=f"authorization=Bearer {secrets[1]}",
        ),
        active_asset={"content": f'DATABASE_PASSWORD="{secrets[3]}"\n'},
        catalogue=CATALOGUE,
        logs=(
            f"Authorization: Bearer {secrets[1]}\n"
            "postgresql://admin:database-password@db.internal/app"
        ),
        recent_test_evidence=[{
            "test": "contracts/extract", "capability_paths": [CAPABILITY],
            "status": "failed", "details": f"token={secrets[2]}",
        }],
    )
    serialized = context.model_dump_json()

    assert all(secret not in serialized for secret in secrets)
    assert "unrelated-private-chat" not in serialized
    assert "another-private-message" not in serialized
    assert "[REDACTED]" in serialized
    assert {artifact.artifact_id for artifact in context.artifacts} == {
        "input-artifact", "11111111-1111-1111-1111-111111111111"
    }
    assert context.redaction.replacement_count >= 7
    assert context.redaction.excluded_fields == ["conversation", "messages"]


class Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Client:
    workspace = "test"

    def __init__(self, job, *, logs="", active=None, job_status=200, log_status=200, active_status=200):
        self.job = job
        self.logs = logs
        self.active = active or {"content": "def main():\n    pass\n"}
        self.job_status = job_status
        self.log_status = log_status
        self.active_status = active_status
        self.paths = []

    def get(self, path, raise_for_status=True):
        self.paths.append(path)
        if "/jobs_u/get_logs/" in path:
            return Response(self.log_status, self.logs, self.logs)
        if "/jobs_u/get/" in path:
            return Response(self.job_status, self.job)
        return Response(self.active_status, self.active)


@pytest.mark.parametrize(("error", "logs", "category"), [
    ("ParserError", "selector not found after markup changed", FailureCategory.source_drift),
    ("ConnectError", "upstream endpoint unavailable", FailureCategory.infrastructure),
    ("psycopg.errors.UndefinedTable", 'relation "product_snapshots" does not exist', FailureCategory.dependency),
])
def test_windmill_integration_classifies_parser_endpoint_and_database_failures(error, logs, category):
    client = Client(failed_job(error=error), logs=logs)
    context = inspect_failure_from_windmill("job-1", catalogue_yaml(), client=client)

    assert context.classification.category is category
    assert client.paths == [
        "/w/test/jobs_u/get/job-1",
        "/w/test/jobs_u/get_logs/job-1",
        f"/w/test/scripts/get/p/{CAPABILITY}",
    ]


def test_unavailable_active_asset_is_visible_but_does_not_discard_job_evidence():
    client = Client(failed_job(), logs="selector not found", active_status=503)
    context = inspect_failure_from_windmill("job-1", catalogue_yaml(), client=client)

    assert context.original_job.job_id == failed_job()["id"]
    assert context.active_capability.code.content == "[active code unavailable]"
    assert context.collection_warnings == ["active script unavailable: HTTP 503"]


def test_unavailable_job_endpoint_fails_without_fabricating_context():
    client = Client(None, job_status=503)
    with pytest.raises(FailureInspectionError, match="could not be loaded: HTTP 503"):
        inspect_failure_from_windmill("job-1", catalogue_yaml(), client=client)


def test_successful_job_is_rejected():
    with pytest.raises(FailureInspectionError, match="only inspects failed jobs"):
        build_repair_context(
            job=failed_job(success=True),
            active_asset={"content": "pass"},
            catalogue=CATALOGUE,
        )


def test_checked_in_repair_context_schema_matches_model():
    assert json.loads(SCHEMA.read_text()) == RepairContext.model_json_schema()
