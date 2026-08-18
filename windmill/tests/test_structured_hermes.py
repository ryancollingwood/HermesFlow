"""HF-019 structured Hermes invocation tests."""
import json
from pathlib import Path
from types import SimpleNamespace

from f.hermes_flow.catalogue.models import load_catalogue
from f.libraries.ai.invoke_hermes_structured import (
    build_request,
    invoke_hermes_structured,
)
from f.libraries.storage.artifacts import FilesystemArtifactStore

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
CATALOGUE_PATH = Path(__file__).parents[1] / "capability-index.yaml"


class Usage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}


class Completions:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            model="hermes-test-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
            usage=Usage(),
        )


class Client:
    def __init__(self, outcomes):
        self.completions = Completions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def invoke(tmp_path, outcomes, **kwargs):
    client = Client(outcomes)
    result = invoke_hermes_structured(
        {"base_url": "http://hermes", "api_key": "top-secret-key"},
        "answer the task",
        [{"role": "user", "content": "prior context"}],
        {"value": 42},
        SCHEMA,
        store=FilesystemArtifactStore(tmp_path),
        client=client,
        **kwargs,
    )
    return result, client


def test_request_construction_uses_openai_json_schema_format():
    request = build_request(
        "do it", [{"role": "assistant", "content": "context"}], {"x": 1}, SCHEMA,
        model="model-a", temperature=0.1, max_tokens=99,
    )
    assert request["model"] == "model-a"
    assert request["temperature"] == 0.1
    assert request["max_tokens"] == 99
    assert request["response_format"]["json_schema"]["schema"] == SCHEMA
    assert request["messages"][-1]["role"] == "user"
    assert '"x": 1' in request["messages"][-1]["content"]


def test_valid_response_is_parsed_validated_and_retained(tmp_path):
    result, client = invoke(tmp_path, ['{"answer":"yes"}'])
    assert result.status == "success"
    assert result.parsed_output == {"answer": "yes"}
    assert result.model == "hermes-test-model"
    assert result.parameters == {"temperature": 0.2, "max_tokens": 2048, "max_retries": 2}
    assert result.usage["total_tokens"] == 13
    assert result.attempts[0].status == "passed"
    assert len(client.completions.requests) == 1
    kinds = [
        FilesystemArtifactStore(tmp_path).read_metadata(item.artifact_id)["metadata"]["kind"]
        for item in result.artifacts
    ]
    assert kinds == ["task_prompt", "conversation", "input_payload", "raw_response", "parsed_output"]


def test_schema_validation_failure_retries_then_succeeds(tmp_path):
    result, client = invoke(tmp_path, ['{"wrong":1}', '{"answer":"fixed"}'], max_retries=1)
    assert result.status == "success"
    assert [attempt.status for attempt in result.attempts] == ["validation_failed", "passed"]
    assert "required property" in result.attempts[0].validation_errors[0]
    assert result.attempts[0].usage["total_tokens"] == 13
    assert result.usage["total_tokens"] == 26
    assert len(client.completions.requests) == 2


def test_invalid_json_and_transport_failure_are_visible_after_exhaustion(tmp_path):
    result, _ = invoke(tmp_path, [RuntimeError("gateway down"), "not-json"], max_retries=1)
    assert result.status == "failure"
    assert result.parsed_output is None
    assert [attempt.status for attempt in result.attempts] == ["request_failed", "validation_failed"]
    assert result.attempts[0].error == "gateway down"
    assert result.attempts[1].error


def test_code_fenced_json_is_accepted(tmp_path):
    result, _ = invoke(tmp_path, ['```json\n{"answer":"yes"}\n```'], max_retries=0)
    assert result.status == "success"


def test_prompt_conversation_input_and_outputs_are_secret_redacted_in_artifacts(tmp_path):
    client = Client(['{"answer":"top-secret-key"}'])
    store = FilesystemArtifactStore(tmp_path)
    result = invoke_hermes_structured(
        {"base_url": "http://hermes", "api_key": "top-secret-key"},
        "use top-secret-key",
        [{"role": "user", "content": "token top-secret-key", "authorization": "Bearer abc"}],
        {"api_key": "another-secret", "nested": "top-secret-key"},
        SCHEMA,
        store=store,
        client=client,
    )
    for artifact in result.artifacts:
        retained = store.read(artifact).decode()
        assert "top-secret-key" not in retained
        assert "another-secret" not in retained
        assert "Bearer abc" not in retained
    assert "[REDACTED]" in store.read(result.artifacts[0]).decode()


def test_lineage_links_raw_response_to_prompt_conversation_and_input(tmp_path):
    result, _ = invoke(tmp_path, ['{"answer":"yes"}'])
    raw_response = next(
        item for item in result.artifacts
        if FilesystemArtifactStore(tmp_path).read_metadata(item.artifact_id)["metadata"]["kind"]
        == "raw_response"
    )
    assert set(raw_response.derived_from) == {
        result.artifacts[0].artifact_id,
        result.artifacts[1].artifact_id,
        result.artifacts[2].artifact_id,
    }


def test_usage_and_attempts_are_json_serializable(tmp_path):
    result, _ = invoke(tmp_path, ['{"answer":"yes"}'])
    payload = json.loads(result.model_dump_json())
    assert payload["attempts"][0]["raw_artifact_id"]
    assert payload["usage"]["prompt_tokens"] == 10


def test_catalogue_marks_wrapper_nondeterministic():
    entry = next(
        item for item in load_catalogue(CATALOGUE_PATH.read_text()).entries
        if item.metadata.path == "f/libraries/ai/invoke_hermes_structured"
    )
    assert entry.metadata.deterministic is False
