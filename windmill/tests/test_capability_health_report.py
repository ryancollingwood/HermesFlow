"""HF-033 capability-health report, metrics, links, and dashboard tests."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from f.hermes_flow.testing.health_report import (
    CapabilityHealthReport,
    Freshness,
    HealthStatus,
    build_capability_health_report,
    prometheus_text,
    write_prometheus_textfile,
)
from f.hermes_flow.testing.scheduled_health import HealthState

CATALOGUE = """schema_version: '1.0'
entries:
  - kind: script
    tags: []
    inputs_summary: none
    outputs_summary: healthy output
    metadata:
      path: f/capabilities/healthy
      capability_version: '1.2.0'
      summary: healthy capability
      maturity: stable
      owners: [platform]
      scheduled_health: {enabled: true}
  - kind: flow
    tags: []
    inputs_summary: none
    outputs_summary: warning output
    metadata:
      path: f/workflows/warning
      capability_version: '2.0.0'
      summary: stale capability
      maturity: experimental
      owners: [operations]
      dependencies: [f/capabilities/healthy]
      scheduled_health: {enabled: true}
  - kind: script
    tags: []
    inputs_summary: none
    outputs_summary: failed output
    metadata:
      path: f/capabilities/failed
      capability_version: '3.0.0'
      summary: failed capability
      maturity: experimental
      owners: [platform]
      dependencies: [f/capabilities/healthy]
      scheduled_health: {enabled: true}
  - kind: script
    tags: []
    inputs_summary: none
    outputs_summary: unknown output
    metadata:
      path: f/capabilities/untested
      capability_version: '1.0.0'
      summary: untested capability
      maturity: stable
      owners: [platform]
"""


class StateReader:
    def __init__(self, states):
        self.states = states

    def load(self, capability_path):
        return self.states.get(capability_path)


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)


def states():
    return {
        "f/capabilities/healthy": HealthState(
            capability_path="f/capabilities/healthy", active_version="1.2.0",
            run_count=5, consecutive_failures=0, last_status="passed",
            last_run_at=NOW - timedelta(minutes=5),
            last_test_ids=["health/healthy"], last_job_ids=["job-healthy"],
            recent_statuses=["passed", "passed", "passed"],
        ),
        "f/workflows/warning": HealthState(
            capability_path="f/workflows/warning", active_version="2.0.0",
            run_count=2, consecutive_failures=0, last_status="passed",
            last_run_at=NOW - timedelta(days=2),
            last_test_ids=["health/warning"], last_job_ids=["job-warning"],
            recent_statuses=["failed", "passed"],
        ),
        "f/capabilities/failed": HealthState(
            capability_path="f/capabilities/failed", active_version="3.0.0",
            run_count=4, consecutive_failures=3, last_status="failed",
            last_run_at=NOW - timedelta(minutes=2),
            last_test_ids=["health/failed"], last_job_ids=["job-failed"],
            recent_statuses=["passed", "failed", "failed", "failed"],
        ),
    }


def report():
    return build_capability_health_report(
        CATALOGUE, state_reader=StateReader(states()), workspace="test",
        windmill_base_url="https://windmill.example", now=NOW,
        stale_after_seconds=86_400,
    )


def test_report_classifies_healthy_warning_failed_and_missing_data():
    by_path = {row.path: row for row in report().capabilities}
    assert by_path["f/capabilities/healthy"].status is HealthStatus.healthy
    assert by_path["f/capabilities/healthy"].freshness is Freshness.fresh
    assert by_path["f/workflows/warning"].status is HealthStatus.warning
    assert by_path["f/workflows/warning"].freshness is Freshness.stale
    assert by_path["f/capabilities/failed"].status is HealthStatus.failed
    assert by_path["f/capabilities/failed"].consecutive_failures == 3
    assert by_path["f/capabilities/failed"].recent_failure_count == 3
    assert by_path["f/workflows/warning"].recent_failure_count == 1
    assert by_path["f/capabilities/untested"].status is HealthStatus.untested
    assert by_path["f/capabilities/untested"].freshness is Freshness.missing
    assert by_path["f/capabilities/untested"].last_test_at is None


def test_report_uses_active_metadata_and_links_authoritative_windmill_assets_jobs_schedules():
    by_path = {row.path: row for row in report().capabilities}
    healthy = by_path["f/capabilities/healthy"]
    warning = by_path["f/workflows/warning"]
    assert healthy.active_version == "1.2.0"
    assert healthy.maturity == "stable"
    assert healthy.asset_url.endswith("/api/w/test/scripts/get/p/f/capabilities/healthy")
    assert healthy.last_job_url.endswith("/api/w/test/jobs_u/get/job-healthy")
    assert healthy.schedule_url.endswith(
        "/api/w/test/schedules/get/f/hermes_flow/health_f_capabilities_healthy"
    )
    assert "/flows/get/f/workflows/warning" in warning.asset_url
    assert healthy.scheduled_dependents == [
        "f/capabilities/failed", "f/workflows/warning"
    ]


def test_active_version_without_current_test_evidence_is_untested():
    changed = states()
    changed["f/capabilities/healthy"].active_version = "1.1.0"
    result = build_capability_health_report(
        CATALOGUE, state_reader=StateReader(changed), now=NOW
    )
    row = next(item for item in result.capabilities if item.path == "f/capabilities/healthy")
    assert row.active_version == "1.2.0"
    assert row.tested_version == "1.1.0"
    assert row.status is HealthStatus.untested
    assert row.freshness is Freshness.missing


def test_prometheus_projection_is_current_snapshot_and_atomically_replaceable(tmp_path):
    current = report()
    text = prometheus_text(current)
    assert 'path="f/capabilities/healthy"' in text
    assert 'status="healthy"' in text
    assert 'last_job_url="https://windmill.example/api/w/test/jobs_u/get/job-failed"' in text
    assert "hermesflow_capability_health" in text
    assert "hermesflow_capability_consecutive_failures" in text
    assert "hermesflow_capability_recent_failures" in text
    assert "hermesflow_capability_report_generated_timestamp_seconds" in text
    target = write_prometheus_textfile(current, tmp_path / "health.prom")
    assert target.read_text() == text
    assert list(tmp_path.glob("*.tmp")) == []


def test_provisioned_dashboard_has_health_freshness_dependencies_and_windmill_links():
    root = Path(__file__).parents[2]
    dashboard = json.loads(
        (root / "grafana/provisioning/dashboards/hermesflow-capability-health.json").read_text()
    )
    encoded = json.dumps(dashboard)
    assert dashboard["uid"] == "hermesflow-capability-health"
    assert "hermesflow_capability_health" in encoded
    assert "hermesflow_capability_report_generated_timestamp_seconds" in encoded
    assert "hermesflow_capability_scheduled_dependents" in encoded
    assert "hermesflow_capability_recent_failures" in encoded
    assert "Open Windmill asset" in encoded
    assert "Open latest Windmill job" in encoded
    compose = yaml.safe_load((root / "docker-compose.observability.yml").read_text())
    node = compose["services"]["node_exporter"]
    assert "--collector.textfile" in node["command"]
    assert "--collector.textfile.directory=/shared/metrics" in node["command"]
    assert any(volume.endswith(":/shared:ro") for volume in node["volumes"])


def test_checked_in_health_report_schema_matches_model():
    schema = Path(__file__).parents[2] / "docs/schemas/capability_health_report.schema.json"
    assert json.loads(schema.read_text()) == CapabilityHealthReport.model_json_schema()
