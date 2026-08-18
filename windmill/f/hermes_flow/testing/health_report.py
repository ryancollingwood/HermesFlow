"""HF-033 authoritative capability-health report and Prometheus projection."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import wmill
from f.hermes_flow.catalogue.models import CapabilityKind, load_catalogue
from f.hermes_flow.testing.scheduled_health import (
    HealthState,
    WindmillHealthStateStore,
    _safe_path,
)
from pydantic import BaseModel, Field

CAPABILITY_PATH = "f/hermes_flow/testing/health_report"
CAPABILITY_VERSION = "1.0.0"
DEFAULT_METRICS_PATH = "/shared/metrics/hermesflow_capabilities.prom"


class HealthStatus(str, Enum):
    healthy = "healthy"
    warning = "warning"
    failed = "failed"
    untested = "untested"


class Freshness(str, Enum):
    fresh = "fresh"
    stale = "stale"
    missing = "missing"


class CapabilityHealthRow(BaseModel):
    path: str
    kind: CapabilityKind
    maturity: str
    active_version: str
    tested_version: str | None = None
    owners: list[str]
    status: HealthStatus
    last_test_status: str | None = None
    last_test_at: datetime | None = None
    age_seconds: int | None = Field(default=None, ge=0)
    freshness: Freshness
    consecutive_failures: int = Field(default=0, ge=0)
    recent_failure_count: int = Field(default=0, ge=0, le=10)
    recent_window_size: int = Field(default=0, ge=0, le=10)
    run_count: int = Field(default=0, ge=0)
    last_test_ids: list[str] = Field(default_factory=list)
    last_job_ids: list[str] = Field(default_factory=list)
    scheduled_health_enabled: bool
    scheduled_dependents: list[str] = Field(default_factory=list)
    asset_url: str
    last_job_url: str | None = None
    schedule_url: str | None = None


class CapabilityHealthReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: datetime
    workspace: str
    windmill_base_url: str
    stale_after_seconds: int = Field(ge=60)
    source: str = "CapabilityMetadata + HF-020 HealthState"
    capabilities: list[CapabilityHealthRow]


class HealthStateReader(Protocol):
    def load(self, capability_path: str) -> HealthState | None: ...


def _asset_url(base_url: str, workspace: str, kind: CapabilityKind, path: str) -> str:
    endpoint = "flows/get" if kind is CapabilityKind.flow else "scripts/get/p"
    return (
        f"{base_url.rstrip('/')}/api/w/{quote(workspace, safe='')}/{endpoint}/"
        f"{quote(path, safe='/')}"
    )


def _job_url(base_url: str, workspace: str, job_id: str) -> str:
    return (
        f"{base_url.rstrip('/')}/api/w/{quote(workspace, safe='')}/jobs_u/get/"
        f"{quote(job_id, safe='')}"
    )


def _schedule_url(base_url: str, workspace: str, capability_path: str) -> str:
    schedule = f"f/hermes_flow/health_{_safe_path(capability_path)}"
    return (
        f"{base_url.rstrip('/')}/api/w/{quote(workspace, safe='')}/schedules/get/"
        f"{quote(schedule, safe='/')}"
    )


def _status(
    state: HealthState | None, active_version: str, now: datetime, stale_after_seconds: int
) -> tuple[HealthStatus, Freshness, int | None]:
    if state is None or state.last_run_at is None:
        return HealthStatus.untested, Freshness.missing, None
    age_seconds = max(0, int((now - state.last_run_at).total_seconds()))
    if state.active_version != active_version:
        return HealthStatus.untested, Freshness.missing, age_seconds
    freshness = (
        Freshness.stale if age_seconds > stale_after_seconds else Freshness.fresh
    )
    if state.last_status == "failed" or state.consecutive_failures > 0:
        return HealthStatus.failed, freshness, age_seconds
    if state.last_status == "passed" and freshness is Freshness.fresh:
        return HealthStatus.healthy, freshness, age_seconds
    return HealthStatus.warning, freshness, age_seconds


def build_capability_health_report(
    catalogue_yaml: str,
    *,
    state_reader: HealthStateReader,
    windmill_base_url: str = "http://windmill.localhost",
    workspace: str = "main",
    stale_after_seconds: int = 86_400,
    now: datetime | None = None,
) -> CapabilityHealthReport:
    if stale_after_seconds < 60:
        raise ValueError("stale_after_seconds must be at least 60")
    catalogue = load_catalogue(catalogue_yaml)
    generated_at = now or datetime.now(timezone.utc)
    reverse_scheduled: dict[str, list[str]] = {}
    for entry in catalogue.entries:
        if not entry.metadata.scheduled_health.enabled:
            continue
        for dependency in entry.metadata.dependencies:
            reverse_scheduled.setdefault(dependency, []).append(entry.metadata.path)

    rows = []
    for entry in sorted(catalogue.entries, key=lambda item: item.metadata.path):
        metadata = entry.metadata
        state = state_reader.load(metadata.path)
        status, freshness, age_seconds = _status(
            state, metadata.capability_version, generated_at, stale_after_seconds
        )
        job_ids = list(state.last_job_ids) if state else []
        rows.append(CapabilityHealthRow(
            path=metadata.path,
            kind=entry.kind,
            maturity=metadata.maturity.value,
            active_version=metadata.capability_version,
            tested_version=state.active_version if state else None,
            owners=metadata.owners,
            status=status,
            last_test_status=state.last_status if state else None,
            last_test_at=state.last_run_at if state else None,
            age_seconds=age_seconds,
            freshness=freshness,
            consecutive_failures=state.consecutive_failures if state else 0,
            recent_failure_count=(
                max(
                    state.recent_statuses.count("failed"),
                    min(state.consecutive_failures, 10),
                ) if state else 0
            ),
            recent_window_size=len(state.recent_statuses) if state else 0,
            run_count=state.run_count if state else 0,
            last_test_ids=list(state.last_test_ids) if state else [],
            last_job_ids=job_ids,
            scheduled_health_enabled=metadata.scheduled_health.enabled,
            scheduled_dependents=sorted(reverse_scheduled.get(metadata.path, [])),
            asset_url=_asset_url(windmill_base_url, workspace, entry.kind, metadata.path),
            last_job_url=(
                _job_url(windmill_base_url, workspace, job_ids[-1]) if job_ids else None
            ),
            schedule_url=(
                _schedule_url(windmill_base_url, workspace, metadata.path)
                if metadata.scheduled_health.enabled else None
            ),
        ))
    return CapabilityHealthReport(
        generated_at=generated_at,
        workspace=workspace,
        windmill_base_url=windmill_base_url.rstrip("/"),
        stale_after_seconds=stale_after_seconds,
        capabilities=rows,
    )


def _escape_label(value: str | None) -> str:
    return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(values: dict[str, object]) -> str:
    return ",".join(
        f'{key}="{_escape_label(str(value) if value is not None else "")}"'
        for key, value in values.items()
    )


def prometheus_text(report: CapabilityHealthReport) -> str:
    status_value = {
        HealthStatus.healthy: 0,
        HealthStatus.warning: 1,
        HealthStatus.failed: 2,
        HealthStatus.untested: 3,
    }
    health_samples = []
    failure_samples = []
    dependent_samples = []
    recent_failure_samples = []
    timestamp_samples = []
    for row in report.capabilities:
        common = {
            "path": row.path,
            "kind": row.kind.value,
            "maturity": row.maturity,
            "active_version": row.active_version,
            "tested_version": row.tested_version,
            "status": row.status.value,
            "last_test_status": row.last_test_status,
            "freshness": row.freshness.value,
            "owners": ",".join(row.owners),
            "scheduled_dependents": ",".join(row.scheduled_dependents),
            "asset_url": row.asset_url,
            "last_job_url": row.last_job_url,
            "schedule_url": row.schedule_url,
        }
        health_samples.append(
            f"hermesflow_capability_health{{{_labels(common)}}} {status_value[row.status]}"
        )
        metric_labels = _labels({"path": row.path})
        failure_samples.append(
            f"hermesflow_capability_consecutive_failures{{{metric_labels}}} "
            f"{row.consecutive_failures}"
        )
        recent_failure_samples.append(
            f"hermesflow_capability_recent_failures{{{metric_labels}}} "
            f"{row.recent_failure_count}"
        )
        dependent_samples.append(
            f"hermesflow_capability_scheduled_dependents{{{metric_labels}}} "
            f"{len(row.scheduled_dependents)}"
        )
        if row.last_test_at:
            timestamp_samples.append(
                f"hermesflow_capability_last_test_timestamp_seconds{{{metric_labels}}} "
                f"{row.last_test_at.timestamp():.0f}"
            )

    lines = [
        "# HELP hermesflow_capability_health Current capability health (0 healthy, 1 warning, 2 failed, 3 untested).",
        "# TYPE hermesflow_capability_health gauge",
        *health_samples,
        "# HELP hermesflow_capability_consecutive_failures Consecutive failed scheduled health runs.",
        "# TYPE hermesflow_capability_consecutive_failures gauge",
        *failure_samples,
        "# HELP hermesflow_capability_recent_failures Failed runs among the latest ten recorded health statuses.",
        "# TYPE hermesflow_capability_recent_failures gauge",
        *recent_failure_samples,
        "# HELP hermesflow_capability_scheduled_dependents Scheduled capabilities depending on this capability.",
        "# TYPE hermesflow_capability_scheduled_dependents gauge",
        *dependent_samples,
        "# HELP hermesflow_capability_last_test_timestamp_seconds Unix timestamp of latest test evidence.",
        "# TYPE hermesflow_capability_last_test_timestamp_seconds gauge",
        *timestamp_samples,
    ]
    lines.extend([
        "# HELP hermesflow_capability_report_generated_timestamp_seconds Unix timestamp of the latest report projection.",
        "# TYPE hermesflow_capability_report_generated_timestamp_seconds gauge",
        f"hermesflow_capability_report_generated_timestamp_seconds {report.generated_at.timestamp():.0f}",
    ])
    return "\n".join(lines) + "\n"


def write_prometheus_textfile(report: CapabilityHealthReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(prometheus_text(report), encoding="utf-8")
    os.replace(temporary, target)
    return target


def generate_health_report(
    catalogue_yaml: str,
    *,
    windmill_base_url: str = "http://windmill.localhost",
    stale_after_seconds: int = 86_400,
    metrics_path: str | Path = DEFAULT_METRICS_PATH,
    client=None,
    now: datetime | None = None,
) -> CapabilityHealthReport:
    windmill = client or wmill.Windmill()
    report = build_capability_health_report(
        catalogue_yaml,
        state_reader=WindmillHealthStateStore(windmill),
        windmill_base_url=windmill_base_url,
        workspace=windmill.workspace,
        stale_after_seconds=stale_after_seconds,
        now=now,
    )
    write_prometheus_textfile(report, metrics_path)
    return report


def main(
    catalogue_yaml: str,
    windmill_base_url: str = "http://windmill.localhost",
    stale_after_seconds: int = 86_400,
) -> dict:
    return generate_health_report(
        catalogue_yaml,
        windmill_base_url=windmill_base_url,
        stale_after_seconds=stale_after_seconds,
    ).model_dump(mode="json")
