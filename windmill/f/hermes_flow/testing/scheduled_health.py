"""HF-020 metadata-driven scheduled capability health checks."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

import wmill
from f.hermes_flow.catalogue.models import Catalogue, CatalogueEntry, load_catalogue
from f.hermes_flow.testing.runner import (
    TestExecutor,
    TestManifest,
    TestMode,
    TestRunResult,
    TestStatus,
    discover_tests,
    load_test_manifests,
    run_tests,
)
from pydantic import BaseModel, Field

RUNNER_PATH = "f/hermes_flow/testing/scheduled_health"
REPORT_PATH = "f/hermes_flow/testing/health_report"
STATE_ROOT = "f/hermes_flow_state/health"


class HealthState(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    active_version: str
    run_count: int = 0
    consecutive_failures: int = 0
    last_status: str | None = None
    last_run_at: datetime | None = None
    last_test_ids: list[str] = Field(default_factory=list)
    last_job_ids: list[str] = Field(default_factory=list)
    recent_statuses: list[str] = Field(default_factory=list, max_length=10)


class HealthFailureRecord(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    active_version: str
    consecutive_failures: int
    escalation_required: bool
    evidence: list[dict[str, Any]]
    recorded_at: datetime


class ScheduledHealthResult(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    active_version: str
    status: str
    sampled_test_ids: list[str]
    consecutive_failures: int
    escalation_required: bool = False
    auto_disabled: bool = False
    test_run: TestRunResult | None = None
    failure_record: HealthFailureRecord | None = None
    details: str | None = None


class HealthStateStore(Protocol):
    def load(self, capability_path: str) -> HealthState | None: ...
    def save(self, state: HealthState) -> None: ...
    def save_failure(self, state: HealthState, record: HealthFailureRecord) -> None: ...


def _safe_path(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", path).strip("_")


def state_path(capability_path: str) -> str:
    return f"{STATE_ROOT}/{_safe_path(capability_path)}"


class WindmillHealthStateStore:
    def __init__(self, client=None):
        self.client = client or wmill.Windmill()

    def _put(self, path: str, value: str, description: str) -> None:
        workspace = self.client.workspace
        existing = self.client.get(
            f"/w/{workspace}/variables/get/{quote(path, safe='/')}", raise_for_status=False
        )
        if existing.status_code == 200:
            response = self.client.post(
                f"/w/{workspace}/variables/update/{quote(path, safe='/')}",
                json={"value": value}, raise_for_status=False,
            )
        elif existing.status_code == 404:
            response = self.client.post(
                f"/w/{workspace}/variables/create",
                json={
                    "path": path,
                    "value": value,
                    "is_secret": False,
                    "description": description,
                },
                raise_for_status=False,
            )
        else:
            raise RuntimeError(f"failed to inspect health state at {path}: {existing.status_code}")
        if response.status_code not in (200, 201):
            raise RuntimeError(f"failed to persist health state at {path}: {response.status_code}")

    def load(self, capability_path: str) -> HealthState | None:
        path = state_path(capability_path)
        response = self.client.get(
            f"/w/{self.client.workspace}/variables/get/{quote(path, safe='/')}",
            raise_for_status=False,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"failed to load health state at {path}: {response.status_code}")
        return HealthState.model_validate_json(response.json()["value"])

    def save(self, state: HealthState) -> None:
        self._put(
            state_path(state.capability_path),
            state.model_dump_json(),
            f"HF-020 scheduled health state for {state.capability_path}",
        )

    def save_failure(self, state: HealthState, record: HealthFailureRecord) -> None:
        path = f"{STATE_ROOT}/failures/{_safe_path(state.capability_path)}_{state.run_count}"
        self._put(path, record.model_dump_json(), f"HF-020 failure record for {state.capability_path}")


def _scheduled_tests(entry: CatalogueEntry, manifest: TestManifest) -> list:
    tests = discover_tests(
        manifest, entry.metadata.path, entry.metadata.test_requirements, TestMode.scheduled
    )
    if entry.metadata.scheduled_health.enabled and not tests:
        raise ValueError(
            f"scheduled health enabled for {entry.metadata.path!r} but no scheduled tests resolved"
        )
    return tests


def build_schedule_definitions(catalogue_yaml: str, manifest_yaml: str) -> list[dict]:
    catalogue = load_catalogue(catalogue_yaml)
    manifest = load_test_manifests(manifest_yaml)
    definitions = []
    for entry in catalogue.entries:
        policy = entry.metadata.scheduled_health
        if not policy.enabled:
            continue
        _scheduled_tests(entry, manifest)
        definitions.append({
            "path": f"f/hermes_flow/health_{_safe_path(entry.metadata.path)}",
            "schedule": policy.cron,
            "timezone": policy.timezone,
            "script_path": RUNNER_PATH,
            "is_flow": False,
            "args": {
                "catalogue_yaml": catalogue_yaml,
                "manifest_yaml": manifest_yaml,
                "capability_path": entry.metadata.path,
            },
            "enabled": True,
            "no_flow_overlap": True,
            "cron_version": "v2",
            "summary": f"HermesFlow health check: {entry.metadata.path}",
            "description": "Generated from CapabilityMetadata.scheduled_health (HF-020).",
            "labels": ["hermesflow", "capability-health"],
        })
    definitions.append({
        "path": "f/hermes_flow/health_dashboard_report",
        "schedule": "0 */5 * * * *",
        "timezone": "UTC",
        "script_path": REPORT_PATH,
        "is_flow": False,
        "args": {"catalogue_yaml": catalogue_yaml},
        "enabled": True,
        "no_flow_overlap": True,
        "cron_version": "v2",
        "summary": "HermesFlow capability health dashboard projection",
        "description": (
            "Generated from CapabilityMetadata and HF-020 test state for HF-033."
        ),
        "labels": ["hermesflow", "capability-health", "grafana"],
    })
    return definitions


def reconcile_schedules(
    catalogue_yaml: str,
    manifest_yaml: str,
    *,
    approved: bool = False,
    client=None,
) -> list[dict]:
    if not approved:
        raise PermissionError("schedule creation/update requires explicit approval")
    windmill = client or wmill.Windmill()
    changes = []
    for definition in build_schedule_definitions(catalogue_yaml, manifest_yaml):
        path = definition["path"]
        exists = windmill.get(
            f"/w/{windmill.workspace}/schedules/exists/{quote(path, safe='/')}",
            raise_for_status=False,
        )
        if exists.status_code != 200:
            raise RuntimeError(f"failed to inspect schedule {path}: {exists.status_code}")
        if exists.json() is True:
            payload = {
                key: value for key, value in definition.items()
                if key not in {"path", "script_path", "is_flow", "enabled"}
            }
            response = windmill.post(
                f"/w/{windmill.workspace}/schedules/update/{quote(path, safe='/')}",
                json=payload, raise_for_status=False,
            )
            action = "updated"
        else:
            response = windmill.post(
                f"/w/{windmill.workspace}/schedules/create",
                json=definition, raise_for_status=False,
            )
            action = "created"
        if response.status_code not in (200, 201):
            verb = "update" if action == "updated" else "create"
            raise RuntimeError(f"failed to {verb} schedule {path}: {response.status_code}")
        changes.append({"path": path, "action": action})
    return changes


def run_scheduled_health(
    catalogue_yaml: str,
    manifest_yaml: str,
    capability_path: str,
    *,
    store: HealthStateStore | None = None,
    executor: TestExecutor | None = None,
    now: datetime | None = None,
) -> ScheduledHealthResult:
    catalogue: Catalogue = load_catalogue(catalogue_yaml)
    entry = catalogue.get(capability_path)
    if entry is None:
        raise ValueError(f"unknown capability {capability_path!r}")
    policy = entry.metadata.scheduled_health
    if not policy.enabled:
        raise ValueError(f"scheduled health is not enabled for {capability_path!r}")
    manifest = load_test_manifests(manifest_yaml)
    scheduled = _scheduled_tests(entry, manifest)
    sample_count = min(policy.max_samples_per_run, policy.rate_limit_per_minute)
    sampled_ids = [test.id for test in scheduled[:sample_count]]
    state_store = store or WindmillHealthStateStore()
    state = state_store.load(capability_path) or HealthState(
        capability_path=capability_path, active_version=entry.metadata.capability_version
    )
    if state.active_version != entry.metadata.capability_version:
        state.active_version = entry.metadata.capability_version
        state.consecutive_failures = 0
        state.last_status = None
        state.last_run_at = None
        state.last_test_ids = []
        state.last_job_ids = []
        state.recent_statuses = []
    current_time = now or datetime.now(timezone.utc)
    minimum_interval = timedelta(minutes=1) / policy.rate_limit_per_minute
    if state.last_run_at and current_time - state.last_run_at < minimum_interval:
        return ScheduledHealthResult(
            capability_path=capability_path,
            active_version=entry.metadata.capability_version,
            status="rate_limited",
            sampled_test_ids=[],
            consecutive_failures=state.consecutive_failures,
            details="health run suppressed by metadata rate limit",
        )

    test_run = run_tests(
        manifest,
        capability_path,
        sampled_ids,
        mode=TestMode.scheduled,
        max_timeout_seconds=policy.max_timeout_seconds,
        max_data_bytes=policy.max_data_bytes,
        executor=executor,
    )
    state.run_count += 1
    state.active_version = entry.metadata.capability_version
    state.last_run_at = current_time
    state.last_test_ids = [item.test for item in test_run.evidence]
    state.last_job_ids = [item.job_id for item in test_run.evidence if item.job_id]
    all_skipped = bool(test_run.evidence) and all(
        item.status is TestStatus.skipped for item in test_run.evidence
    )
    failure_record = None
    if not test_run.passed:
        state.consecutive_failures += 1
        status = "failed"
        escalation_required = state.consecutive_failures >= policy.escalate_after_failures
        failure_record = HealthFailureRecord(
            capability_path=capability_path,
            active_version=entry.metadata.capability_version,
            consecutive_failures=state.consecutive_failures,
            escalation_required=escalation_required,
            evidence=[item.model_dump(mode="json") for item in test_run.evidence],
            recorded_at=current_time,
        )
        state_store.save_failure(state, failure_record)
    elif all_skipped:
        status = "skipped"
        escalation_required = False
    else:
        status = "passed"
        state.consecutive_failures = 0
        escalation_required = False
    state.last_status = status
    state.recent_statuses = [*state.recent_statuses, status][-10:]
    state_store.save(state)
    return ScheduledHealthResult(
        capability_path=capability_path,
        active_version=entry.metadata.capability_version,
        status=status,
        sampled_test_ids=sampled_ids,
        consecutive_failures=state.consecutive_failures,
        escalation_required=escalation_required,
        auto_disabled=False,
        test_run=test_run,
        failure_record=failure_record,
    )


def main(catalogue_yaml: str, manifest_yaml: str, capability_path: str) -> dict:
    result = run_scheduled_health(catalogue_yaml, manifest_yaml, capability_path)
    try:
        # Lazy import avoids a module cycle: health_report reads HealthState.
        from f.hermes_flow.testing.health_report import generate_health_report

        generate_health_report(catalogue_yaml)
    except Exception as exc:
        projection = (
            f"dashboard projection failed ({type(exc).__name__}); "
            "health state remains authoritative"
        )
        result.details = f"{result.details}; {projection}" if result.details else projection
    return result.model_dump(mode="json")
