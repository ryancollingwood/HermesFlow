"""HF-020 metadata-driven scheduled health tests."""
from datetime import datetime, timedelta, timezone

import pytest

from f.hermes_flow.testing.scheduled_health import (
    build_schedule_definitions,
    reconcile_schedules,
    run_scheduled_health,
    state_path,
)


CATALOGUE = """
schema_version: "1.0"
entries:
  - kind: script
    tags: [example]
    inputs_summary: none
    outputs_summary: health result
    metadata:
      path: f/capabilities/example
      capability_version: "2.4.0"
      summary: example capability
      maturity: stable
      owners: [platform]
      test_requirements: [health/one, health/two]
      scheduled_health:
        enabled: true
        cron: "0 17 * * * *"
        timezone: UTC
        max_samples_per_run: 1
        max_data_bytes: 1000
        max_timeout_seconds: 10
        rate_limit_per_minute: 1
        escalate_after_failures: 3
"""

MANIFEST = """
schema_version: "1.0"
tests:
  - id: health/one
    capability_paths: [f/capabilities/example]
    type: live_integration
    mode: scheduled
    script_path: f/tests/health_one
    timeout_seconds: 10
    max_data_bytes: 1000
  - id: health/two
    capability_paths: [f/capabilities/example]
    type: smoke
    mode: scheduled
    script_path: f/tests/health_two
    timeout_seconds: 10
    max_data_bytes: 1000
"""


class FakeExecutor:
    def __init__(self, outcome="pass"):
        self.outcome = outcome
        self.ran = []

    def run(self, spec):
        self.ran.append(spec.id)
        return f"job-{len(self.ran)}", {"status": self.outcome, "details": self.outcome}


class MemoryStore:
    def __init__(self):
        self.state = None
        self.failures = []

    def load(self, capability_path):
        assert capability_path == "f/capabilities/example"
        return self.state.model_copy(deep=True) if self.state else None

    def save(self, state):
        self.state = state.model_copy(deep=True)

    def save_failure(self, state, record):
        self.failures.append(record.model_copy(deep=True))


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class FakeWindmill:
    workspace = "disposable"

    def __init__(self, exists=False):
        self.exists = exists
        self.posts = []

    def get(self, path, **kwargs):
        assert path.startswith("/w/disposable/schedules/exists/")
        return Response(200, self.exists)

    def post(self, path, json, **kwargs):
        self.posts.append((path, json))
        return Response(201)


def test_schedule_definition_is_generated_from_metadata():
    definitions = build_schedule_definitions(CATALOGUE, MANIFEST)
    assert len(definitions) == 2
    schedule = definitions[0]
    assert schedule["schedule"] == "0 17 * * * *"
    assert schedule["timezone"] == "UTC"
    assert schedule["script_path"] == "f/hermes_flow/testing/scheduled_health"
    assert schedule["no_flow_overlap"] is True
    assert schedule["args"]["capability_path"] == "f/capabilities/example"
    report = definitions[1]
    assert report["path"] == "f/hermes_flow/health_dashboard_report"
    assert report["script_path"] == "f/hermes_flow/testing/health_report"
    assert report["schedule"] == "0 */5 * * * *"
    assert report["args"] == {"catalogue_yaml": CATALOGUE}


def test_schedule_reconciliation_requires_explicit_approval():
    with pytest.raises(PermissionError, match="explicit approval"):
        reconcile_schedules(CATALOGUE, MANIFEST, client=FakeWindmill())


def test_schedule_creation_in_disposable_workspace():
    client = FakeWindmill()
    changes = reconcile_schedules(CATALOGUE, MANIFEST, approved=True, client=client)
    assert changes[0] == {
        "path": "f/hermes_flow/health_f_capabilities_example",
        "action": "created",
    }
    assert changes[1]["path"] == "f/hermes_flow/health_dashboard_report"
    path, payload = client.posts[0]
    assert path == "/w/disposable/schedules/create"
    assert payload["enabled"] is True
    assert payload["cron_version"] == "v2"


def test_existing_schedule_is_updated_without_recreating_or_reenabling_it():
    client = FakeWindmill(exists=True)
    changes = reconcile_schedules(CATALOGUE, MANIFEST, approved=True, client=client)
    assert changes[0]["action"] == "updated"
    path, payload = client.posts[0]
    assert path.startswith("/w/disposable/schedules/update/")
    assert "path" not in payload
    assert "enabled" not in payload


def test_health_run_bounds_sample_count():
    store = MemoryStore()
    executor = FakeExecutor()
    result = run_scheduled_health(
        CATALOGUE, MANIFEST, "f/capabilities/example",
        store=store, executor=executor,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert result.status == "passed"
    assert result.sampled_test_ids == ["health/one"]
    assert executor.ran == ["health/one"]
    assert store.state.last_test_ids == ["health/one"]
    assert store.state.last_job_ids == ["job-1"]
    assert store.state.recent_statuses == ["passed"]


def test_three_failures_create_versioned_escalation_without_auto_disable():
    store = MemoryStore()
    executor = FakeExecutor("fail")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    results = [
        run_scheduled_health(
            CATALOGUE, MANIFEST, "f/capabilities/example",
            store=store, executor=executor, now=started + timedelta(minutes=index),
        )
        for index in range(3)
    ]
    assert [result.consecutive_failures for result in results] == [1, 2, 3]
    assert [result.escalation_required for result in results] == [False, False, True]
    assert results[-1].auto_disabled is False
    assert results[-1].failure_record.capability_path == "f/capabilities/example"
    assert results[-1].failure_record.active_version == "2.4.0"
    assert len(store.failures) == 3


def test_recovery_resets_consecutive_failure_count():
    store = MemoryStore()
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failed = run_scheduled_health(
        CATALOGUE, MANIFEST, "f/capabilities/example",
        store=store, executor=FakeExecutor("fail"), now=started,
    )
    recovered = run_scheduled_health(
        CATALOGUE, MANIFEST, "f/capabilities/example",
        store=store, executor=FakeExecutor("pass"), now=started + timedelta(minutes=1),
    )
    assert failed.consecutive_failures == 1
    assert recovered.status == "passed"
    assert recovered.consecutive_failures == 0
    assert store.state.last_status == "passed"
    assert store.state.recent_statuses == ["failed", "passed"]


def test_rate_limit_suppresses_early_repeat_without_mutating_state():
    store = MemoryStore()
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run_scheduled_health(
        CATALOGUE, MANIFEST, "f/capabilities/example",
        store=store, executor=FakeExecutor(), now=started,
    )
    executor = FakeExecutor()
    limited = run_scheduled_health(
        CATALOGUE, MANIFEST, "f/capabilities/example",
        store=store, executor=executor, now=started + timedelta(seconds=30),
    )
    assert limited.status == "rate_limited"
    assert executor.ran == []
    assert store.state.run_count == 1


def test_runtime_state_is_outside_synced_control_plane_path():
    assert state_path("f/capabilities/example") == (
        "f/hermes_flow_state/health/f_capabilities_example"
    )
    assert not state_path("f/capabilities/example").startswith("f/hermes_flow/")
