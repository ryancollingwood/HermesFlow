"""HF-030 repair-candidate generation and policy-gate tests."""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from f.hermes_flow.candidate_ops.models import CandidateRecord, metadata_variable_path
from f.hermes_flow.repair.generate_candidate import (
    RepairGenerationError,
    RepairGenerationResult,
    build_generation_prompt,
    generate_repair_candidate,
)
from f.hermes_flow.repair.models import (
    ActiveCapabilityEvidence,
    BoundedDocument,
    FailureCategory,
    FailureClassification,
    OriginalJob,
    RedactionSummary,
    RepairContext,
    RepairContextLimits,
    TruncationSummary,
)
from f.libraries.storage.artifacts import FilesystemArtifactStore

ACTIVE_PATH = "f/capabilities/collection/source_selector"
ACTIVE_HASH = "active-windmill-hash"
ACTIVE_CODE = '''def select_products(document):
    return document.select(".old-card")
'''
PATCHED_CODE = '''def select_products(document):
    return document.select(".product-card")
'''
RESULT_SCHEMA = Path(__file__).parents[2] / "docs/schemas/repair_generation_result.schema.json"


def document(content: str, *, truncated: bool = False) -> BoundedDocument:
    size = len(content.encode())
    return BoundedDocument(
        content=content,
        original_bytes=size,
        retained_bytes=size,
        truncated=truncated,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def repair_context(*, code: str = ACTIVE_CODE, truncated: bool = False) -> RepairContext:
    return RepairContext(
        original_job=OriginalJob(
            job_id="failed-selector-job",
            workspace="main",
            path=ACTIVE_PATH,
            api_url="http://windmill.localhost/run/failed-selector-job",
        ),
        failure_summary="ParserError: selector not found after markup changed",
        classification=FailureClassification(
            category=FailureCategory.source_drift,
            confidence=0.85,
            reasons=["source parser or selector no longer matched"],
        ),
        active_capability=ActiveCapabilityEvidence(
            path=ACTIVE_PATH,
            capability_version="1.2.3",
            windmill_hash=ACTIVE_HASH,
            code=document(code, truncated=truncated),
        ),
        inputs=document('{"url":"https://example.test/products"}'),
        logs=document("ParserError: selector .old-card not found"),
        redaction=RedactionSummary(),
        truncation=TruncationSummary(
            truncated_sections=["active_code"] if truncated else []
        ),
        total_bytes=1000,
        limits=RepairContextLimits(),
    )


CATALOGUE = f'''schema_version: "1.0"
entries:
  - kind: script
    tags: [collection, selector]
    inputs_summary: HTML document
    outputs_summary: selected product nodes
    input_kinds: [html_document]
    output_kinds: [product_nodes]
    metadata:
      path: {ACTIVE_PATH}
      capability_version: "1.2.3"
      summary: Select product nodes from a source document.
      maturity: stable
      deterministic: true
      owners: [collection]
      effects: {{}}
'''


@dataclass
class FakeResponse:
    status_code: int
    payload: dict = field(default_factory=dict)

    def json(self):
        return self.payload

    @property
    def text(self):
        return str(self.payload)


class FakeWindmill:
    def __init__(self):
        self.workspace = "main"
        self.scripts = {
            ACTIVE_PATH: {
                "hash": ACTIVE_HASH,
                "content": ACTIVE_CODE,
                "language": "python3",
            }
        }
        self.variables = {}

    def get(self, path, raise_for_status=True):
        prefix = "/w/main/scripts/get/p/"
        if path.startswith(prefix):
            script_path = path[len(prefix):]
            return (
                FakeResponse(200, self.scripts[script_path])
                if script_path in self.scripts else FakeResponse(404)
            )
        variable_prefix = "/w/main/variables/get/"
        if path.startswith(variable_prefix):
            variable_path = path[len(variable_prefix):]
            return (
                FakeResponse(200, {"value": self.variables[variable_path]})
                if variable_path in self.variables else FakeResponse(404)
            )
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json, raise_for_status=True):
        if path == "/w/main/scripts/create":
            self.scripts[json["path"]] = {
                "hash": f"candidate-{len(self.scripts)}",
                "content": json["content"],
                "language": json["language"],
            }
            return FakeResponse(201)
        if path == "/w/main/variables/create":
            self.variables[json["path"]] = json["value"]
            return FakeResponse(201)
        raise AssertionError(f"unexpected POST {path}")


class Usage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


class Completions:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        content = next(self.outcomes)
        return SimpleNamespace(
            model="hermes-repair-test",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=Usage(),
        )


class HermesClient:
    def __init__(self, outcomes):
        self.completions = Completions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def proposal(content: str = PATCHED_CODE, *, test_updates=True) -> str:
    payload = {
        "summary": "Update the drifted product selector",
        "rationale": "The source now uses .product-card.",
        "patched_content": content,
    }
    if test_updates:
        payload["test_updates"] = [{
            "test_id": "source-selector-new-markup",
            "failure_reproduction": "Fixture with .product-card reproduces the old miss.",
            "proposed_change": "Assert the selector returns the product node.",
        }]
    return json.dumps(payload)


def run_generation(tmp_path, output, **kwargs):
    windmill = kwargs.pop("candidate_client", FakeWindmill())
    hermes = kwargs.pop("hermes_client", HermesClient([output]))
    store = FilesystemArtifactStore(tmp_path)
    result = generate_repair_candidate(
        {"base_url": "http://hermes", "api_key": "test-secret"},
        kwargs.pop("context", repair_context()),
        CATALOGUE,
        max_retries=kwargs.pop("max_retries", 0),
        candidate_client=windmill,
        hermes_client=hermes,
        store=store,
        **kwargs,
    )
    return result, windmill, hermes, store


def artifact_by_kind(result, store, kind):
    return next(
        artifact for artifact in result.generation_artifacts
        if store.read_metadata(artifact.artifact_id)["metadata"]["kind"] == kind
    )


def test_source_selector_repair_creates_isolated_candidate_with_full_provenance(tmp_path):
    result, windmill, _, _ = run_generation(tmp_path, proposal())
    assert result.status == "candidate_created"
    assert windmill.scripts[ACTIVE_PATH]["content"] == ACTIVE_CODE
    assert result.candidate["path"] != ACTIVE_PATH
    assert windmill.scripts[result.candidate["path"]]["content"] == PATCHED_CODE
    record = CandidateRecord.model_validate_json(
        windmill.variables[metadata_variable_path(result.candidate["candidate_id"])]
    )
    assert record.source_path == ACTIVE_PATH
    assert record.base_version == ACTIVE_HASH
    assert record.failed_job_id == "failed-selector-job"
    assert record.repair_context_sha256 == result.repair_context_sha256
    assert record.generation_trace_id == result.generation_trace_id
    assert record.generation_artifact_ids == [
        str(item.artifact_id) for item in result.generation_artifacts
    ]


def test_exact_prompt_context_and_model_outputs_are_retained(tmp_path):
    output = proposal()
    context = repair_context()
    result, _, hermes, store = run_generation(tmp_path, output, context=context)
    prompt = build_generation_prompt(context)
    assert "smallest change" in prompt
    assert "test update" in prompt
    assert "active-code mutation" in prompt
    assert store.read(artifact_by_kind(result, store, "task_prompt")).decode() == prompt
    assert json.loads(store.read(artifact_by_kind(result, store, "conversation"))) == []
    assert json.loads(store.read(artifact_by_kind(result, store, "input_payload"))) == context.model_dump(mode="json")
    assert store.read(artifact_by_kind(result, store, "raw_response")).decode() == output
    assert json.loads(store.read(artifact_by_kind(result, store, "parsed_output")))["patched_content"] == PATCHED_CODE
    assert hermes.completions.requests[0]["temperature"] == 0


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("not-json", "not parseable"),
        (proposal(test_updates=False), "repair schema"),
    ],
)
def test_invalid_outputs_are_retained_and_rejected_without_candidate(tmp_path, output, reason):
    result, windmill, _, store = run_generation(tmp_path, output)
    assert result.status == "rejected"
    assert reason in result.rejection_reason
    assert len(windmill.scripts) == 1
    assert artifact_by_kind(result, store, "task_prompt")
    assert artifact_by_kind(result, store, "input_payload")
    assert artifact_by_kind(result, store, "raw_response")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ('import subprocess\ndef select_products(document):\n    return subprocess.run(["curl"])\n', "unsafe"),
        ('from subprocess import run as harmless\ndef select_products(document):\n    return harmless(["curl"])\n', "unsafe"),
        ('def select_products(document)\n    return document.select(".product-card")\n', "not parseable"),
        ('import made_up_repair_sdk\n' + PATCHED_CODE, "third-party"),
        ('API_KEY = "hardcoded"\n' + PATCHED_CODE, "credential literals"),
    ],
)
def test_unsafe_or_unparseable_patches_are_rejected(tmp_path, content, reason):
    result, windmill, _, _ = run_generation(tmp_path, proposal(content))
    assert result.status == "rejected"
    assert reason in result.rejection_reason
    assert len(windmill.scripts) == 1
    assert windmill.scripts[ACTIVE_PATH]["content"] == ACTIVE_CODE


def test_nonminimal_patch_is_rejected(tmp_path):
    content = PATCHED_CODE + "\n".join(f"unused_{index} = {index}" for index in range(20))
    result, windmill, _, _ = run_generation(
        tmp_path, proposal(content), max_changed_lines=5, max_change_ratio=1
    )
    assert result.status == "rejected"
    assert "minimal-change limit" in result.rejection_reason
    assert len(windmill.scripts) == 1


def test_stale_active_hash_is_rejected_before_model_invocation(tmp_path):
    windmill = FakeWindmill()
    windmill.scripts[ACTIVE_PATH]["hash"] = "new-active-hash"
    hermes = HermesClient([proposal()])
    with pytest.raises(RepairGenerationError, match="hash changed"):
        run_generation(tmp_path, proposal(), candidate_client=windmill, hermes_client=hermes)
    assert hermes.completions.requests == []
    assert len(windmill.scripts) == 1


def test_truncated_active_source_is_rejected_before_model_invocation(tmp_path):
    hermes = HermesClient([proposal()])
    with pytest.raises(RepairGenerationError, match="truncated"):
        run_generation(
            tmp_path,
            proposal(),
            context=repair_context(truncated=True),
            hermes_client=hermes,
        )
    assert hermes.completions.requests == []


def test_invalid_minimal_change_limits_fail_before_model_invocation(tmp_path):
    hermes = HermesClient([proposal()])
    with pytest.raises(RepairGenerationError, match="max_change_ratio"):
        run_generation(tmp_path, proposal(), max_change_ratio=2, hermes_client=hermes)
    assert hermes.completions.requests == []


def test_checked_in_repair_generation_result_schema_matches_model():
    assert json.loads(RESULT_SCHEMA.read_text()) == RepairGenerationResult.model_json_schema()
