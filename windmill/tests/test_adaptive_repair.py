"""HF-032 end-to-end adaptive repair orchestration tests."""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from f.hermes_flow.candidate_ops.models import CandidateRecord, metadata_variable_path
from f.hermes_flow.repair.finalize_retry import RetryRecord, finalize_and_retry
from f.hermes_flow.repair.models import (
    ActiveCapabilityEvidence, BoundedDocument, FailureCategory,
    FailureClassification, OriginalJob, RedactionSummary, RepairContext,
    RepairContextLimits, TruncationSummary,
)
from f.hermes_flow.repair.orchestrate import (
    AttemptLimitExceeded, RepairPreparation, prepare_adaptive_repair,
)
from f.hermes_flow.testing.source_drift_fixture import run_source_drift_fixture
from f.libraries.lineage.models import ArtifactStage, ExecutionContext
from f.libraries.storage.artifacts import FilesystemArtifactStore


ACTIVE = "f/capabilities/retail/select_products"
CONSUMER = "f/workflows/retail_collection"
CANDIDATE_ID = "a" * 16
CANDIDATE = f"f/hermes_flow/candidates/{CANDIDATE_ID}"
ACTIVE_CODE = 'def main(source_html):\n    return {"matched_count": source_html.count("old-card")}\n'
PATCHED_CODE = 'def main(source_html):\n    return {"matched_count": source_html.count("product-card")}\n'
NEW_HTML = '<html><article class="product-card" data-token="secret">New product</article></html>'

CATALOGUE = f"""schema_version: '1.0'
entries:
  - kind: script
    tags: [retail]
    inputs_summary: HTML
    outputs_summary: matched products
    metadata:
      path: {ACTIVE}
      capability_version: '1.0.0'
      summary: Select retail products
      maturity: stable
      owners: [retail]
      test_requirements: [retail-selector-contract]
  - kind: flow
    tags: [retail]
    inputs_summary: retail source
    outputs_summary: collection
    metadata:
      path: {CONSUMER}
      capability_version: '1.0.0'
      summary: Retail collection workflow
      maturity: stable
      owners: [retail]
      dependencies: [{ACTIVE}]
      test_requirements: [retail-collection-smoke]
"""

MANIFEST = f"""schema_version: '1.0'
tests:
  - id: retail-selector-contract
    capability_paths: [{ACTIVE}]
    type: contract
    mode: promotion_gating
    script_path: f/tests/retail_selector_contract
  - id: retail-collection-smoke
    capability_paths: [{CONSUMER}]
    type: smoke
    mode: promotion_gating
    script_path: f/tests/retail_collection_smoke
"""


def document(value):
    return BoundedDocument(
        content=value, original_bytes=len(value.encode()), retained_bytes=len(value.encode()),
        sha256=hashlib.sha256(value.encode()).hexdigest(),
    )


def repair_context(category=FailureCategory.source_drift):
    return RepairContext(
        original_job=OriginalJob(
            job_id="failed-retail-job", workspace="main", path=ACTIVE,
            api_url="http://windmill/api/w/main/jobs_u/get/failed-retail-job",
        ),
        failure_summary="selector not found after retail markup changed",
        classification=FailureClassification(category=category, confidence=.9, reasons=["test"]),
        active_capability=ActiveCapabilityEvidence(
            path=ACTIVE, capability_version="1.0.0", windmill_hash="active-v1",
            code=document(ACTIVE_CODE),
        ),
        inputs=document(json.dumps({"source_html": NEW_HTML})), logs=document("selector not found"),
        redaction=RedactionSummary(), truncation=TruncationSummary(),
        limits=RepairContextLimits(), total_bytes=1000,
    )


@dataclass
class Response:
    status_code: int
    body: object = field(default_factory=dict)

    def json(self): return self.body
    @property
    def text(self): return str(self.body)


class FakeWindmill:
    workspace = "main"

    def __init__(self):
        self.scripts = {
            ACTIVE: {
                "hash": "active-v1", "content": ACTIVE_CODE, "language": "python3",
                "schema": {}, "summary": "selector", "description": "",
            },
        }
        self.variables = {}
        self.job = {
            "id": "failed-retail-job", "script_path": ACTIVE, "success": False,
            "args": {"source_html": NEW_HTML},
            "error": {"message": "selector not found after retail markup changed"},
            "logs": "selector not found after retail markup changed",
        }
        self.runs = []

    def get(self, path, raise_for_status=True):
        if path == "/w/main/jobs_u/get/failed-retail-job":
            return Response(200, self.job)
        if path.startswith("/w/main/scripts/get/p/"):
            item = self.scripts.get(path.removeprefix("/w/main/scripts/get/p/"))
            return Response(200, item) if item else Response(404)
        if path.startswith("/w/main/variables/get/"):
            value = self.variables.get(path.removeprefix("/w/main/variables/get/"))
            return Response(200, {"value": value}) if value else Response(404)
        raise AssertionError(path)

    def post(self, path, json, raise_for_status=True):
        if path == "/w/main/variables/create":
            if json["path"] in self.variables:
                return Response(409, "exists")
            self.variables[json["path"]] = json["value"]
            return Response(201)
        if path.startswith("/w/main/variables/update/"):
            key = path.removeprefix("/w/main/variables/update/")
            if key not in self.variables:
                return Response(404)
            self.variables[key] = json["value"]
            return Response(200)
        if path == "/w/main/scripts/create":
            current = self.scripts[json["path"]]
            if current["hash"] != json["parent_hash"]:
                return Response(409, "stale")
            self.scripts[json["path"]] = {**current, **json, "hash": "active-v2"}
            return Response(201)
        raise AssertionError(path)

    def run_script_by_path_async(self, path, args):
        job_id = f"retry-{len(self.runs) + 1}"
        self.runs.append((job_id, path, args))
        return job_id

    def wait_job(self, job_id, timeout, cleanup):
        args = next(args for found, _, args in self.runs if found == job_id)
        return {"matched_count": args.get("source_html", "").count("product-card")}


class RegressionExecutor:
    def __init__(self, windmill, store, failing=None):
        self.windmill, self.store, self.failing = windmill, store, failing
        self.ran = []

    def run(self, spec):
        self.ran.append(spec.id)
        if spec.id == self.failing:
            return f"test-{spec.id}", {"status": "fail", "details": "deliberate failure"}
        if spec.id.startswith("fixture/source-drift/"):
            result = run_source_drift_fixture(
                spec.args["fixture_record"], spec.args["candidate_path"],
                client=self.windmill, store=self.store,
            )
            return f"test-{spec.id}", result.model_dump(mode="json")
        return f"test-{spec.id}", {"status": "pass"}


def source_artifact(store):
    return store.write(
        NEW_HTML, trace_id=uuid4(), stage=ArtifactStage.raw,
        creator_capability="f/capabilities/retail/fetch", creator_capability_version="1.0.0",
        media_type="text/html",
    )


def install_generated_candidate(fake):
    fake.scripts[CANDIDATE] = {
        "hash": "candidate-v1", "content": PATCHED_CODE, "language": "python3",
        "schema": {}, "summary": "selector", "description": "",
    }
    record = CandidateRecord(
        candidate_id=CANDIDATE_ID, path=CANDIDATE, request_key="repair-request",
        reason="repair retail selector", source_path=ACTIVE, base_version="active-v1",
        failed_job_id="failed-retail-job", repair_context_sha256="0" * 64,
        generation_trace_id=str(uuid4()), generation_artifact_ids=[str(uuid4())],
    )
    fake.variables[metadata_variable_path(CANDIDATE_ID)] = record.model_dump_json()
    return SimpleNamespace(
        status="candidate_created", candidate=record.model_dump(mode="json"),
        rejection_reason=None,
    )


def prepare(monkeypatch, tmp_path, *, failing=None, category=FailureCategory.source_drift, fake=None):
    import f.hermes_flow.repair.orchestrate as module
    windmill = fake or FakeWindmill()
    store = FilesystemArtifactStore(tmp_path)
    executor = RegressionExecutor(windmill, store, failing=failing)
    monkeypatch.setattr(module, "inspect_failure_from_windmill", lambda *a, **k: repair_context(category))
    monkeypatch.setattr(module, "generate_repair_candidate", lambda *a, **k: install_generated_candidate(windmill))
    context = ExecutionContext(
        capability=ACTIVE, capability_version="active-v1", initiating_actor="schedule",
        request_id="original-request",
    )
    result = prepare_adaptive_repair(
        {"base_url": "http://hermes", "api_key": "test"}, "failed-retail-job",
        CATALOGUE, MANIFEST, context, client=windmill, test_executor=executor, store=store,
        source_artifact=source_artifact(store),
        expected_behavior={"description": "new retail card is selected", "expected_values": {"matched_count": 1}},
        fixture_binding={"fixture_argument": "source_html", "payload_mode": "text"},
    )
    return result, windmill, executor, context


def test_changed_retail_fixture_and_affected_consumer_gate_approval(monkeypatch, tmp_path):
    result, _, executor, _ = prepare(monkeypatch, tmp_path)
    assert result["status"] == "ready_for_approval"
    assert {"retail-collection-smoke", "retail-selector-contract"} <= set(executor.ran)
    assert any(item.startswith("fixture/source-drift/") for item in executor.ran)
    assert "product-card" in result["promotion"]["evidence"]["diff"]["code"]["unified_diff"]
    assert result["promotion"]["evidence"]["impact"]["consumers"][0]["path"] == CONSUMER


def test_policy_denial_stops_before_generation(monkeypatch, tmp_path):
    import f.hermes_flow.repair.orchestrate as module
    fake = FakeWindmill()
    monkeypatch.setattr(module, "inspect_failure_from_windmill", lambda *a, **k: repair_context(FailureCategory.infrastructure))
    monkeypatch.setattr(module, "generate_repair_candidate", lambda *a, **k: pytest.fail("generation ran"))
    context = ExecutionContext(capability=ACTIVE, capability_version="active-v1", initiating_actor="schedule")
    result = prepare_adaptive_repair({}, "failed-retail-job", CATALOGUE, MANIFEST, context, client=fake)
    assert result["status"] == "policy_denied"
    assert CANDIDATE not in fake.scripts


def test_failed_affected_consumer_test_blocks_approval(monkeypatch, tmp_path):
    result, fake, _, _ = prepare(monkeypatch, tmp_path, failing="retail-collection-smoke")
    assert result["status"] == "tests_failed"
    assert result["promotion"] is None
    assert fake.scripts[ACTIVE]["hash"] == "active-v1"


def test_rejected_approval_neither_promotes_nor_retries(monkeypatch, tmp_path):
    prepared, fake, _, _ = prepare(monkeypatch, tmp_path)
    result = finalize_and_retry(prepared, False, "u/reviewer", client=fake)
    assert result["status"] == "approval_rejected"
    assert fake.scripts[ACTIVE]["hash"] == "active-v1"
    assert fake.runs[-1][1] == CANDIDATE  # fixture run only


def test_stale_active_version_blocks_promotion_and_retry(monkeypatch, tmp_path):
    prepared, fake, _, _ = prepare(monkeypatch, tmp_path)
    fake.scripts[ACTIVE]["hash"] = "concurrent-v2"
    before = len(fake.runs)
    result = finalize_and_retry(prepared, True, "u/reviewer", client=fake)
    assert result["status"] == "stale_conflict"
    assert len(fake.runs) == before


def test_approved_repair_promotes_then_retries_with_parent_trace(monkeypatch, tmp_path):
    prepared, fake, _, parent = prepare(monkeypatch, tmp_path)
    before = len(fake.runs)
    result = finalize_and_retry(prepared, True, "u/reviewer", client=fake)
    assert result["status"] == "retry_succeeded"
    assert fake.scripts[ACTIVE]["hash"] == "active-v2"
    assert len(fake.runs) == before + 1
    _, path, args = fake.runs[-1]
    assert path == ACTIVE
    assert args["context"]["parent_trace_id"] == str(parent.trace_id)
    assert result["failed_job_id"] == "failed-retail-job"
    assert result["candidate_id"] == CANDIDATE_ID
    assert result["retry_result_sha256"]


def test_attempt_limit_prevents_infinite_repair_loop(monkeypatch, tmp_path):
    prepared, fake, _, parent = prepare(monkeypatch, tmp_path)
    assert prepared["attempt"] == 1
    second, _, _, _ = prepare(monkeypatch, tmp_path, fake=fake)
    assert second["attempt"] == 2
    with pytest.raises(AttemptLimitExceeded, match="exhausted"):
        prepare_adaptive_repair({}, "failed-retail-job", CATALOGUE, MANIFEST, parent, client=fake)


def test_checked_in_adaptive_repair_schemas_match_models():
    root = Path(__file__).parents[2] / "docs/schemas"
    assert json.loads((root / "repair_preparation.schema.json").read_text()) == (
        RepairPreparation.model_json_schema()
    )
    assert json.loads((root / "adaptive_retry_record.schema.json").read_text()) == (
        RetryRecord.model_json_schema()
    )


def test_flow_uses_native_approval_and_skips_terminal_preparations():
    path = (
        Path(__file__).parents[1]
        / "f/hermes_flow/repair/adaptive_repair.flow/flow.yaml"
    )
    flow = yaml.safe_load(path.read_text())
    modules = {item["id"]: item for item in flow["value"]["modules"]}
    assert modules["approval"]["suspend"]["user_auth_required"] is True
    assert modules["approval"]["suspend"]["self_approval_disabled"] is True
    assert modules["approval"]["skip_if"]["expr"] == (
        'results.prepare.status !== "ready_for_approval"'
    )
    assert modules["finalize_retry"]["skip_if"] == modules["approval"]["skip_if"]
