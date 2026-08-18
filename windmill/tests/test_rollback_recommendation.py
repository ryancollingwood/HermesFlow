"""HF-034 automatic rollback recommendation tests."""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest
from f.hermes_flow.candidate_ops.rollback_recommendation import (
    RollbackRecommendation,
    RollbackRecommendationError,
    execute_approved_rollback,
    execution_variable_path,
    recommend_rollback,
)
from f.hermes_flow.testing.scheduled_health import (
    HealthFailureRecord,
    HealthState,
    WindmillHealthStateStore,
)


@dataclass
class Response:
    status_code: int
    body: object = None

    def json(self): return self.body
    @property
    def text(self): return str(self.body)


@dataclass
class FakeWindmill:
    workspace: str = "main"
    scripts: dict = field(default_factory=lambda: {
        "f/capabilities/base": {
            "hash": "broken-v2", "content": "broken", "language": "python3", "schema": {},
        },
    })
    versions: dict = field(default_factory=lambda: {
        "good-v1": {"hash": "good-v1", "content": "good", "language": "python3", "schema": {}},
    })
    variables: dict = field(default_factory=dict)
    schedules: dict = field(default_factory=lambda: {
        "f/capabilities/base": [{"path": "s/base", "script_path": "f/capabilities/base"}],
        "f/workflows/consumer": [{"path": "s/consumer", "script_path": "f/workflows/consumer"}],
    })
    history: list = field(default_factory=lambda: ["good-v1", "broken-v2"])

    def get(self, path, raise_for_status=True):
        prefix = "/w/main/scripts/get/p/"
        if path.startswith(prefix):
            value = self.scripts.get(path[len(prefix):])
            return Response(200, value) if value else Response(404)
        prefix = "/w/main/scripts/get/h/"
        if path.startswith(prefix):
            value = self.versions.get(path[len(prefix):])
            return Response(200, value) if value else Response(404)
        if path.startswith("/w/main/schedules/list?path="):
            encoded = path.split("?path=", 1)[1].split("&", 1)[0]
            return Response(200, self.schedules.get(unquote(encoded), []))
        prefix = "/w/main/variables/get/"
        if path.startswith(prefix):
            key = unquote(path[len(prefix):])
            if key in self.variables:
                return Response(200, {"value": self.variables[key]})
            return Response(404)
        raise AssertionError(path)

    def post(self, path, json, raise_for_status=True):
        if path == "/w/main/scripts/create":
            current = self.scripts[json["path"]]
            if json["parent_hash"] != current["hash"]:
                return Response(409, "conflict")
            new_hash = "rollback-v3"
            self.scripts[json["path"]] = {**current, **json, "hash": new_hash}
            self.versions[new_hash] = self.scripts[json["path"]]
            self.history.append(new_hash)
            return Response(201, new_hash)
        if path == "/w/main/variables/create":
            self.variables[json["path"]] = json["value"]
            return Response(201)
        prefix = "/w/main/variables/update/"
        if path.startswith(prefix):
            key = unquote(path[len(prefix):])
            self.variables[key] = json["value"]
            return Response(200)
        raise AssertionError(path)


class FakeExecutor:
    def __init__(self, outcome="pass"):
        self.outcome = outcome
        self.ran = []

    def run(self, spec):
        self.ran.append(spec.id)
        return f"job-{len(self.ran)}", {"status": self.outcome}


CATALOGUE = """
schema_version: '1.0'
entries:
  - kind: script
    tags: [base]
    inputs_summary: input
    outputs_summary: output
    metadata:
      path: f/capabilities/base
      capability_version: '2.0.0'
      summary: base
      maturity: stable
      owners: [platform]
      effects:
        network: true
      test_requirements: [tests/base-smoke]
      scheduled_health:
        enabled: true
        escalate_after_failures: 3
  - kind: flow
    tags: [consumer]
    inputs_summary: input
    outputs_summary: output
    metadata:
      path: f/workflows/consumer
      capability_version: '1.0.0'
      summary: consumer
      maturity: stable
      owners: [platform]
      dependencies: [f/capabilities/base]
      test_requirements: [tests/consumer-smoke]
"""

MANIFEST = """
schema_version: '1.0'
tests:
  - id: tests/base-smoke
    capability_paths: [f/capabilities/base]
    type: smoke
    mode: promotion_gating
    script_path: f/tests/base_smoke
  - id: tests/consumer-smoke
    capability_paths: [f/workflows/consumer]
    type: smoke
    mode: promotion_gating
    script_path: f/tests/consumer_smoke
"""


def _seed_failures(fake, capability_path, active_version, details_by_run):
    """Persist a HealthState + one HealthFailureRecord per failing run, oldest first,
    exactly as f.hermes_flow.testing.scheduled_health.run_scheduled_health would."""
    store = WindmillHealthStateStore(fake)
    state = HealthState(capability_path=capability_path, active_version=active_version)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, details in enumerate(details_by_run, start=1):
        state.run_count = index
        state.consecutive_failures = index
        state.last_status = "failed"
        state.recent_statuses = (state.recent_statuses + ["failed"])[-10:]
        state.last_test_ids = ["tests/base-smoke"]
        record = HealthFailureRecord(
            capability_path=capability_path,
            active_version=active_version,
            consecutive_failures=index,
            escalation_required=index >= 3,
            evidence=[{"test": "tests/base-smoke", "status": "failed", "details": details}],
            recorded_at=started + timedelta(minutes=index),
        )
        store.save_failure(state, record)
    store.save(state)
    return state


def test_no_health_evidence_does_not_recommend():
    fake = FakeWindmill()
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.recommended is False
    assert recommendation.threshold_met is False
    assert "no scheduled health evidence" in recommendation.reason


def test_healthy_capability_does_not_recommend():
    fake = FakeWindmill()
    store = WindmillHealthStateStore(fake)
    store.save(HealthState(capability_path="f/capabilities/base", active_version="2.0.0"))
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.recommended is False
    assert recommendation.consecutive_failures == 0
    assert "currently healthy" in recommendation.reason


def test_below_threshold_does_not_recommend():
    fake = FakeWindmill()
    _seed_failures(fake, "f/capabilities/base", "2.0.0", ["TypeError: boom", "TypeError: boom"])
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.threshold_met is False
    assert recommendation.recommended is False
    assert "has not yet reached" in recommendation.reason


def test_transient_infrastructure_failures_are_not_recommended():
    fake = FakeWindmill()
    _seed_failures(fake, "f/capabilities/base", "2.0.0", [
        "Connection refused: endpoint unavailable",
        "Gateway timeout calling endpoint unavailable",
        "connection refused, endpoint unavailable again",
    ])
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.threshold_met is True
    assert recommendation.transient_only is True
    assert recommendation.recommended is False
    assert "transient" in recommendation.reason


def test_recommends_rollback_after_non_transient_regression():
    fake = FakeWindmill()
    _seed_failures(fake, "f/capabilities/base", "2.0.0", [
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
    ])
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.threshold_met is True
    assert recommendation.transient_only is False
    assert recommendation.recommended is True
    assert recommendation.current_evidence.failure_category == "code_defect"
    assert len(recommendation.previous_evidence) == 2
    assert recommendation.required_tests == ["tests/base-smoke", "tests/consumer-smoke"]
    assert recommendation.affected_workflows == ["f/workflows/consumer"]
    assert [s["path"] for s in recommendation.affected_schedules] == ["s/base", "s/consumer"]
    assert recommendation.has_side_effects is True
    assert recommendation.requires_approval is True


def test_mixed_categories_are_not_treated_as_purely_transient():
    fake = FakeWindmill()
    _seed_failures(fake, "f/capabilities/base", "2.0.0", [
        "Connection refused: endpoint unavailable",
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
    ])
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.transient_only is False
    assert recommendation.recommended is True


def _recommended_dict(fake):
    _seed_failures(fake, "f/capabilities/base", "2.0.0", [
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
    ])
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    assert recommendation.recommended is True
    return recommendation.model_dump(mode="json")


def passing_tests():
    return [
        {"test": "tests/base-smoke", "passed": True, "job_id": "prior-1"},
        {"test": "tests/consumer-smoke", "passed": True, "job_id": "prior-2"},
    ]


def test_execute_refuses_when_not_recommended():
    fake = FakeWindmill()
    recommendation = recommend_rollback(CATALOGUE, "f/capabilities/base", client=fake)
    with pytest.raises(RollbackRecommendationError, match="did not recommend"):
        execute_approved_rollback(
            recommendation, CATALOGUE, MANIFEST, "good-v1", "manual", "job-rb",
            passing_tests(), approval_granted=True, approved_by="ops",
            acknowledge_side_effects=True, client=fake,
        )


def test_execute_records_rejection_without_writing_when_approval_denied():
    fake = FakeWindmill()
    recommended = _recommended_dict(fake)
    result = execute_approved_rollback(
        recommended, CATALOGUE, MANIFEST, "good-v1", "manual", "job-rb",
        passing_tests(), approval_granted=False, client=fake,
    )
    assert result["status"] == "approval_rejected"
    assert fake.scripts["f/capabilities/base"]["hash"] == "broken-v2"
    assert execution_variable_path("f/capabilities/base", "job-rb") in fake.variables


def test_execute_requires_approver_identity():
    fake = FakeWindmill()
    recommended = _recommended_dict(fake)
    with pytest.raises(RollbackRecommendationError, match="approver identity"):
        execute_approved_rollback(
            recommended, CATALOGUE, MANIFEST, "good-v1", "manual", "job-rb",
            passing_tests(), approval_granted=True, approved_by=None,
            acknowledge_side_effects=True, client=fake,
        )


def test_execute_is_never_silent_about_side_effects_and_schedules():
    fake = FakeWindmill()
    recommended = _recommended_dict(fake)
    with pytest.raises(RollbackRecommendationError, match="never silent"):
        execute_approved_rollback(
            recommended, CATALOGUE, MANIFEST, "good-v1", "manual", "job-rb",
            passing_tests(), approval_granted=True, approved_by="ops", client=fake,
        )
    assert fake.scripts["f/capabilities/base"]["hash"] == "broken-v2"


def test_approved_rollback_restores_history_and_reruns_impacted_tests():
    fake = FakeWindmill()
    recommended = _recommended_dict(fake)
    executor = FakeExecutor("pass")
    result = execute_approved_rollback(
        recommended, CATALOGUE, MANIFEST, "good-v1", "promotion broke", "job-rb",
        passing_tests(), approval_granted=True, approved_by="ops",
        acknowledge_side_effects=True, executor=executor, client=fake,
    )
    assert result["status"] == "rollback_succeeded"
    assert result["verification_passed"] is True
    assert fake.scripts["f/capabilities/base"]["content"] == "good"
    assert fake.history == ["good-v1", "broken-v2", "rollback-v3"]
    assert result["rollback"]["record"]["failed_version"] == "broken-v2"
    assert result["rollback"]["record"]["rollback_version"] == "rollback-v3"
    assert sorted(executor.ran) == ["tests/base-smoke", "tests/consumer-smoke"]
    assert result["verification"]["passed"] is True
    stored = json.loads(fake.variables[execution_variable_path("f/capabilities/base", "job-rb")])
    assert stored["status"] == "rollback_succeeded"


def test_verification_failure_after_rollback_flags_manual_follow_up_but_keeps_rollback():
    fake = FakeWindmill()
    recommended = _recommended_dict(fake)
    executor = FakeExecutor("fail")
    result = execute_approved_rollback(
        recommended, CATALOGUE, MANIFEST, "good-v1", "promotion broke", "job-rb",
        passing_tests(), approval_granted=True, approved_by="ops",
        acknowledge_side_effects=True, executor=executor, client=fake,
    )
    assert result["status"] == "verification_failed"
    assert result["verification_passed"] is False
    assert "manual" in result["details"]
    # The rollback write itself is not undone even though verification failed.
    assert fake.scripts["f/capabilities/base"]["content"] == "good"
    assert fake.history == ["good-v1", "broken-v2", "rollback-v3"]


def test_execute_without_impacted_tests_skips_verification():
    fake = FakeWindmill()
    fake.schedules = {}
    minimal_catalogue = """
schema_version: '1.0'
entries:
  - kind: script
    tags: [solo]
    inputs_summary: input
    outputs_summary: output
    metadata:
      path: f/capabilities/base
      capability_version: '2.0.0'
      summary: base
      maturity: stable
      owners: [platform]
      scheduled_health:
        enabled: true
        escalate_after_failures: 3
"""
    _seed_failures(fake, "f/capabilities/base", "2.0.0", [
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
        "TypeError: object has no attribute",
    ])
    recommendation = recommend_rollback(minimal_catalogue, "f/capabilities/base", client=fake)
    assert recommendation.recommended is True
    assert recommendation.required_tests == []
    assert recommendation.has_side_effects is False
    assert recommendation.affected_workflows == []
    result = execute_approved_rollback(
        recommendation.model_dump(mode="json"), minimal_catalogue, MANIFEST, "good-v1",
        "promotion broke", "job-rb", [], approval_granted=True, approved_by="ops", client=fake,
    )
    assert result["status"] == "rollback_succeeded"
    assert result["verification"] is None
    assert result["verification_passed"] is True


def test_checked_in_schema_matches_model():
    schema = Path(__file__).parents[2] / "docs/schemas/rollback_recommendation.schema.json"
    assert json.loads(schema.read_text()) == RollbackRecommendation.model_json_schema()
