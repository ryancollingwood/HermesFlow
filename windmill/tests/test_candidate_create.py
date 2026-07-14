"""Unit tests for f/hermes_flow/candidate_ops/{models,create}.py — not synced to
Windmill (see conftest.py). Exercises create_candidate() against an in-memory fake
Windmill client (see FakeWindmill below); live-server integration proof is documented
in architecture/adr/0002-capability-lifecycle.md and this module's own create.py
docstring."""
import json
import pathlib
from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from f.hermes_flow.candidate_ops.create import CandidateCreationError, create_candidate
from f.hermes_flow.candidate_ops.models import (
    CANDIDATES_ROOT,
    CandidateRecord,
    compute_candidate_id,
    compute_candidate_path,
    metadata_variable_path,
)

SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"


@dataclass
class FakeResponse:
    status_code: int
    _json: dict = field(default_factory=dict)

    def json(self) -> dict:
        return self._json

    @property
    def text(self) -> str:
        return str(self._json)


class FakeWindmill:
    """In-memory stand-in for wmill.Windmill() — just enough of .get/.post to
    exercise create_candidate()'s logic without a live server. All calls here
    behave as raise_for_status=False (never raises, always returns a
    FakeResponse) — create_candidate() always passes that explicitly for the
    same reason, matching the real wmill.Windmill client's default-raising
    behaviour discovered live (see create.py's WindmillAdminClient docstring)."""

    def __init__(self):
        self.workspace = "main"
        self.scripts: dict[str, dict] = {}
        self.variables: dict[str, str] = {}

    def get(self, path: str, raise_for_status: bool = True) -> FakeResponse:
        if path.startswith("/w/main/scripts/get/p/"):
            p = path[len("/w/main/scripts/get/p/") :]
            return FakeResponse(200, self.scripts[p]) if p in self.scripts else FakeResponse(404, {})
        if path.startswith("/w/main/variables/get/"):
            p = path[len("/w/main/variables/get/") :]
            return (
                FakeResponse(200, {"value": self.variables[p]})
                if p in self.variables
                else FakeResponse(404, {})
            )
        raise ValueError(f"unexpected GET {path}")

    def post(self, path: str, json: dict, raise_for_status: bool = True) -> FakeResponse:
        if path == "/w/main/scripts/create":
            self.scripts[json["path"]] = {"hash": f"fakehash-{json['path']}", "content": json["content"]}
            return FakeResponse(201, {})
        if path == "/w/main/variables/create":
            self.variables[json["path"]] = json["value"]
            return FakeResponse(201, {})
        raise ValueError(f"unexpected POST {path}")


# ── Identifier and path generation ───────────────────────────────────────────


def test_candidate_id_is_deterministic_for_the_same_request_key():
    assert compute_candidate_id("my-request") == compute_candidate_id("my-request")


def test_candidate_id_differs_for_different_request_keys():
    assert compute_candidate_id("request-a") != compute_candidate_id("request-b")


def test_candidate_id_rejects_empty_request_key():
    with pytest.raises(ValueError, match="must not be empty"):
        compute_candidate_id("")


def test_candidate_path_is_under_the_candidates_root():
    candidate_id = compute_candidate_id("x")
    path = compute_candidate_path(candidate_id)
    assert path.startswith(CANDIDATES_ROOT + "/")
    assert path == f"{CANDIDATES_ROOT}/{candidate_id}"


def test_metadata_variable_path_is_distinct_from_the_script_path():
    candidate_id = compute_candidate_id("x")
    assert metadata_variable_path(candidate_id) != compute_candidate_path(candidate_id)
    assert metadata_variable_path(candidate_id).startswith(CANDIDATES_ROOT + "/")


# ── CandidateRecord validation ───────────────────────────────────────────────


def test_derived_candidate_requires_base_version():
    candidate_id = compute_candidate_id("x")
    with pytest.raises(ValidationError, match="base_version is required"):
        CandidateRecord(
            candidate_id=candidate_id,
            path=compute_candidate_path(candidate_id),
            request_key="x",
            reason="derived without a base version",
            source_path="f/capabilities/some/thing",
        )


def test_record_rejects_a_path_that_does_not_match_its_candidate_id():
    candidate_id = compute_candidate_id("x")
    with pytest.raises(ValidationError, match="does not match candidate_id"):
        CandidateRecord(
            candidate_id=candidate_id,
            path="f/hermes_flow/candidates/some-other-id",
            request_key="x",
            reason="mismatched path",
        )


def test_repair_candidate_rejects_partial_generation_provenance():
    candidate_id = compute_candidate_id("repair-x")
    with pytest.raises(ValidationError, match="repair candidate provenance is incomplete"):
        CandidateRecord(
            candidate_id=candidate_id,
            path=compute_candidate_path(candidate_id),
            request_key="repair-x",
            reason="partial repair provenance",
            source_path="f/capabilities/source-selector",
            base_version="active-hash",
            failed_job_id="failed-job",
        )


# ── Creating new candidates ───────────────────────────────────────────────────


def test_create_new_candidate_writes_a_script_and_metadata_variable():
    fake = FakeWindmill()
    result = create_candidate(
        request_key="req-1", reason="a new capability", content="def main(): return 1", client=fake
    )
    assert result["idempotent"] is False
    assert result["path"] in fake.scripts
    assert metadata_variable_path(result["candidate_id"]) in fake.variables


def test_created_candidate_path_is_isolated_under_candidates_root():
    fake = FakeWindmill()
    result = create_candidate(request_key="req-2", reason="isolation check", content="...", client=fake)
    assert result["path"].startswith(CANDIDATES_ROOT + "/")


def test_created_candidate_metadata_round_trips():
    fake = FakeWindmill()
    result = create_candidate(
        request_key="req-3",
        reason="round trip check",
        content="...",
        conversation_id="conv-abc",
        request_id="req-xyz",
        generated_by_capability="f/libraries/ai/invoke_hermes_structured",
        source_path="f/capabilities/source-selector",
        base_version="active-hash",
        failed_job_id="job-failed-1",
        repair_context_sha256="a" * 64,
        generation_trace_id="trace-1",
        generation_artifact_ids=["artifact-prompt", "artifact-output"],
        client=fake,
    )
    stored = CandidateRecord.model_validate_json(fake.variables[metadata_variable_path(result["candidate_id"])])
    assert stored.reason == "round trip check"
    assert stored.conversation_id == "conv-abc"
    assert stored.request_id == "req-xyz"
    assert stored.generated_by_capability == "f/libraries/ai/invoke_hermes_structured"
    assert stored.failed_job_id == "job-failed-1"
    assert stored.repair_context_sha256 == "a" * 64
    assert stored.generation_trace_id == "trace-1"
    assert stored.generation_artifact_ids == ["artifact-prompt", "artifact-output"]


# ── Deriving candidates from an active version ───────────────────────────────


def test_derived_candidate_records_source_path_and_base_version():
    fake = FakeWindmill()
    fake.scripts["f/capabilities/web/fetch"] = {"hash": "active-hash-42"}
    result = create_candidate(
        request_key="req-derive-1",
        reason="patch web_fetch",
        content="...",
        source_path="f/capabilities/web/fetch",
        client=fake,
    )
    assert result["source_path"] == "f/capabilities/web/fetch"
    assert result["base_version"] == "active-hash-42"


def test_derived_candidate_can_have_base_version_supplied_explicitly():
    fake = FakeWindmill()
    fake.scripts["f/capabilities/web/fetch"] = {"hash": "current-hash"}
    result = create_candidate(
        request_key="req-derive-2",
        reason="patch pinned to an older base",
        content="...",
        source_path="f/capabilities/web/fetch",
        base_version="pinned-older-hash",
        client=fake,
    )
    # Explicit base_version wins — no lookup needed/performed against the (possibly
    # newer) live source.
    assert result["base_version"] == "pinned-older-hash"


def test_deriving_from_a_nonexistent_source_path_raises():
    fake = FakeWindmill()
    with pytest.raises(CandidateCreationError, match="does not exist"):
        create_candidate(
            request_key="req-derive-bad",
            reason="derive from nothing",
            content="...",
            source_path="f/capabilities/does/not/exist",
            client=fake,
        )


# ── Idempotency / duplicate request handling ─────────────────────────────────


def test_duplicate_request_key_returns_the_existing_candidate_idempotently():
    fake = FakeWindmill()
    first = create_candidate(request_key="dup-key", reason="first", content="v1", client=fake)
    second = create_candidate(request_key="dup-key", reason="first", content="v1", client=fake)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["candidate_id"] == second["candidate_id"]
    assert first["path"] == second["path"]


def test_duplicate_request_does_not_create_a_second_script_or_variable():
    fake = FakeWindmill()
    create_candidate(request_key="dup-key-2", reason="first", content="v1", client=fake)
    create_candidate(request_key="dup-key-2", reason="first", content="v1", client=fake)
    assert len(fake.scripts) == 1
    assert len(fake.variables) == 1


def test_different_request_keys_create_different_candidates():
    fake = FakeWindmill()
    a = create_candidate(request_key="key-a", reason="a", content="...", client=fake)
    b = create_candidate(request_key="key-b", reason="b", content="...", client=fake)
    assert a["candidate_id"] != b["candidate_id"]
    assert len(fake.scripts) == 2


def test_idempotent_replay_ignores_a_changed_reason_and_returns_the_original():
    # Same request_key must resolve to the same candidate regardless of what the
    # caller passes the second time — the request_key, not the other args, is the
    # idempotency key.
    fake = FakeWindmill()
    create_candidate(request_key="dup-key-3", reason="original reason", content="v1", client=fake)
    second = create_candidate(request_key="dup-key-3", reason="a different reason", content="v2", client=fake)
    assert second["idempotent"] is True
    assert second["reason"] == "original reason"


# ── Verify active assets are unchanged ────────────────────────────────────────


def test_creating_a_candidate_never_touches_an_existing_active_script():
    fake = FakeWindmill()
    fake.scripts["f/capabilities/web/fetch"] = {"hash": "active-hash", "content": "ORIGINAL CONTENT"}
    before = dict(fake.scripts["f/capabilities/web/fetch"])
    create_candidate(
        request_key="derive-no-mutate",
        reason="derive without mutating the source",
        content="candidate content, not the source's",
        source_path="f/capabilities/web/fetch",
        client=fake,
    )
    assert fake.scripts["f/capabilities/web/fetch"] == before


# ── Path-escape defence ───────────────────────────────────────────────────────


def test_create_candidate_refuses_to_write_outside_the_candidates_root(monkeypatch):
    import f.hermes_flow.candidate_ops.create as create_module

    monkeypatch.setattr(create_module, "compute_candidate_path", lambda candidate_id: "f/capabilities/escaped")
    fake = FakeWindmill()
    with pytest.raises(CandidateCreationError, match="escaped"):
        create_candidate(request_key="escape-attempt", reason="x", content="...", client=fake)
    assert fake.scripts == {}


# ── docs/CI: checked-in JSON Schema export must match the model ─────────────


def test_checked_in_json_schema_matches_model():
    schema_path = SCHEMAS_DIR / "candidate_record.schema.json"
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        "python -c \"import json; from f.hermes_flow.candidate_ops.models import CandidateRecord; "
        'print(json.dumps(CandidateRecord.model_json_schema(), indent=2, sort_keys=True))" '
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(CandidateRecord.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to CandidateRecord — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
