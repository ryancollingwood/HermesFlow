"""HF-034 automatic rollback recommendation and approved, verified execution.

``recommend_rollback`` is read-only: it compares the current HF-020 scheduled
health failure streak against its own recent history (not just the latest
run) and only recommends a rollback once the escalation threshold is met AND
the failure evidence is not exclusively transient infrastructure noise (an
external outage is not a reason to roll back working code). It never writes
anything.

``execute_approved_rollback`` is the only write step. It refuses to run
unless a recommendation actually endorsed the rollback, an approver identity
is supplied, and — whenever the capability has side effects or affects other
workflows/schedules — the caller has explicitly acknowledged that. It then
delegates the write itself to HF-014's ``rollback_capability`` (which keeps
the failed version in Windmill's own version history; nothing is deleted)
and reruns the capability's impacted tests against the now-restored active
version to verify the rollback actually fixed things.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import quote

import wmill
from f.hermes_flow.candidate_ops.diff import _get_script
from f.hermes_flow.candidate_ops.lifecycle import _impact, rollback_capability
from f.hermes_flow.candidate_ops.promote import WindmillPromotionClient
from f.hermes_flow.catalogue.models import load_catalogue
from f.hermes_flow.repair.inspection import classify_failure
from f.hermes_flow.repair.models import FailureCategory
from f.hermes_flow.testing.regression import run_regression_tests
from f.hermes_flow.testing.runner import TestExecutor, load_test_manifests
from f.hermes_flow.testing.scheduled_health import (
    STATE_ROOT,
    HealthFailureRecord,
    WindmillHealthStateStore,
    _safe_path,
)
from pydantic import BaseModel, Field


class RollbackRecommendationError(ValueError):
    pass


class HealthStateReader(Protocol):
    def load(self, capability_path: str) -> Any: ...


class HealthEvidenceSnapshot(BaseModel):
    run_count: int = Field(ge=1)
    consecutive_failures: int = Field(ge=0)
    failure_category: str | None = None
    recorded_at: datetime | None = None
    test_ids: list[str] = Field(default_factory=list)


class RollbackRecommendation(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    active_capability_version: str
    active_script_version: str
    consecutive_failures: int = Field(ge=0)
    escalate_after_failures: int = Field(ge=1)
    threshold_met: bool
    transient_only: bool
    recommended: bool
    reason: str
    current_evidence: HealthEvidenceSnapshot | None = None
    previous_evidence: list[HealthEvidenceSnapshot] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    affected_workflows: list[str] = Field(default_factory=list)
    affected_schedules: list[dict] = Field(default_factory=list)
    has_side_effects: bool
    requires_approval: bool = True
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RollbackExecutionRecord(BaseModel):
    schema_version: str = "1.0"
    status: Literal[
        "approval_rejected", "rollback_succeeded", "verification_failed",
    ]
    capability_path: str
    restore_version: str | None = None
    reason: str
    initiating_job_id: str
    approved_by: str | None = None
    rollback: dict | None = None
    verification: dict | None = None
    verification_passed: bool | None = None
    details: str | None = None
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def execution_variable_path(capability_path: str, initiating_job_id: str) -> str:
    return (
        f"f/hermes_flow_state/rollback_recommendation/"
        f"{_safe_path(capability_path)}_{initiating_job_id}"
    )


def _failure_record_path(capability_path: str, run_count: int) -> str:
    return f"{STATE_ROOT}/failures/{_safe_path(capability_path)}_{run_count}"


def _load_failure_record(
    client: WindmillPromotionClient, path: str
) -> HealthFailureRecord | None:
    response = client.get(
        f"/w/{client.workspace}/variables/get/{quote(path, safe='/')}", raise_for_status=False
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RollbackRecommendationError(
            f"failed to load failure evidence at {path}: {response.status_code}"
        )
    return HealthFailureRecord.model_validate_json(response.json()["value"])


def _recent_failure_records(
    client: WindmillPromotionClient, capability_path: str, run_count: int, limit: int
) -> list[tuple[int, HealthFailureRecord]]:
    """Most-recent-first. Each failing run in a consecutive streak has its own
    record at a deterministic, run_count-keyed path (see ``save_failure`` in
    ``scheduled_health.py``) — no listing endpoint is needed to find them."""
    found: list[tuple[int, HealthFailureRecord]] = []
    for offset in range(limit):
        candidate_run = run_count - offset
        if candidate_run < 1:
            break
        record = _load_failure_record(client, _failure_record_path(capability_path, candidate_run))
        if record is not None:
            found.append((candidate_run, record))
    return found


def _classify_record(record: HealthFailureRecord) -> FailureCategory:
    parts: list[str] = []
    for item in record.evidence:
        parts.append(str(item.get("details") or ""))
        parts.append(str(item.get("test") or ""))
    return classify_failure(*parts).category


def recommend_rollback(
    catalogue_yaml: str,
    capability_path: str,
    *,
    health_store: HealthStateReader | None = None,
    client: WindmillPromotionClient | None = None,
    failure_lookback: int = 10,
) -> RollbackRecommendation:
    w = client or wmill.Windmill()
    catalogue = load_catalogue(catalogue_yaml)
    entry = catalogue.get(capability_path)
    if entry is None:
        raise RollbackRecommendationError(f"unknown capability {capability_path!r}")

    store = health_store or WindmillHealthStateStore(w)
    state = store.load(capability_path)
    active = _get_script(w, capability_path)
    impact = _impact(w, catalogue, capability_path)
    policy = entry.metadata.scheduled_health
    base = dict(
        capability_path=capability_path,
        active_capability_version=entry.metadata.capability_version,
        active_script_version=str(active.get("hash") or ""),
        escalate_after_failures=policy.escalate_after_failures,
        required_tests=impact["required_tests"],
        affected_workflows=impact["workflows"],
        affected_schedules=impact["schedules"],
        has_side_effects=not entry.metadata.effects.is_side_effect_free,
    )

    if state is None or state.active_version != entry.metadata.capability_version:
        return RollbackRecommendation(
            **base, consecutive_failures=0, threshold_met=False, transient_only=False,
            recommended=False,
            reason="no scheduled health evidence has been recorded for the active version yet",
        )
    if state.consecutive_failures == 0:
        return RollbackRecommendation(
            **base, consecutive_failures=0, threshold_met=False, transient_only=False,
            recommended=False, reason="capability is currently healthy; no rollback needed",
        )

    threshold_met = state.consecutive_failures >= policy.escalate_after_failures
    if not threshold_met:
        return RollbackRecommendation(
            **base, consecutive_failures=state.consecutive_failures,
            threshold_met=False, transient_only=False, recommended=False,
            reason=(
                f"consecutive_failures={state.consecutive_failures} has not yet reached "
                f"escalate_after_failures={policy.escalate_after_failures}"
            ),
        )

    pairs = _recent_failure_records(
        w, capability_path, state.run_count, min(state.consecutive_failures, failure_lookback)
    )
    categories = [_classify_record(record) for _, record in pairs]
    transient_only = bool(categories) and all(
        category is FailureCategory.infrastructure for category in categories
    )
    snapshots = [
        HealthEvidenceSnapshot(
            run_count=run_count,
            consecutive_failures=record.consecutive_failures,
            failure_category=category.value,
            recorded_at=record.recorded_at,
            test_ids=sorted({str(item.get("test")) for item in record.evidence if item.get("test")}),
        )
        for (run_count, record), category in zip(pairs, categories)
    ]
    current_evidence = snapshots[0] if snapshots else None
    previous_evidence = snapshots[1:]
    recommended = threshold_met and not transient_only
    if recommended:
        observed = sorted({category.value for category in categories})
        reason = (
            f"consecutive_failures={state.consecutive_failures} meets "
            f"escalate_after_failures={policy.escalate_after_failures}; recent failure "
            f"evidence is not exclusively transient infrastructure issues "
            f"(categories observed: {observed})"
        )
    else:
        reason = (
            f"the last {len(categories)} failing run(s) all classify as transient "
            "infrastructure issues; avoiding a rollback recommendation for what looks "
            "like an external outage rather than a code regression"
        )
    return RollbackRecommendation(
        **base, consecutive_failures=state.consecutive_failures, threshold_met=threshold_met,
        transient_only=transient_only, recommended=recommended, reason=reason,
        current_evidence=current_evidence, previous_evidence=previous_evidence,
    )


def _persist_execution_record(
    client: WindmillPromotionClient, capability_path: str, job_id: str, record: RollbackExecutionRecord
) -> None:
    response = client.post(
        f"/w/{client.workspace}/variables/create",
        json={
            "path": execution_variable_path(capability_path, job_id),
            "value": record.model_dump_json(),
            "is_secret": False,
            "description": f"HF-034 rollback execution record for {capability_path}",
        },
        raise_for_status=False,
    )
    if response.status_code not in (200, 201):
        raise RollbackRecommendationError(
            f"rollback outcome {record.status!r} could not be persisted: {response.status_code}"
        )


def execute_approved_rollback(
    recommendation: dict | RollbackRecommendation,
    catalogue_yaml: str,
    manifest_yaml: str,
    restore_version: str,
    reason: str,
    initiating_job_id: str,
    test_results: list[dict],
    approval_granted: bool,
    approved_by: str | None = None,
    *,
    acknowledge_side_effects: bool = False,
    expected_current_version: str | None = None,
    executor: TestExecutor | None = None,
    client: WindmillPromotionClient | None = None,
) -> dict:
    w = client or wmill.Windmill()
    rec = (
        recommendation if isinstance(recommendation, RollbackRecommendation)
        else RollbackRecommendation.model_validate(recommendation)
    )
    if not rec.recommended:
        raise RollbackRecommendationError(
            "recommend_rollback did not recommend a rollback for this capability; "
            "refusing to execute automatically"
        )
    if not approval_granted:
        record = RollbackExecutionRecord(
            status="approval_rejected", capability_path=rec.capability_path, reason=reason,
            initiating_job_id=initiating_job_id, approved_by=approved_by,
            details="rollback approval was not granted; no changes were made",
        )
        _persist_execution_record(w, rec.capability_path, initiating_job_id, record)
        return record.model_dump(mode="json")
    if not approved_by:
        raise RollbackRecommendationError(
            "rollback approval is missing an authenticated approver identity"
        )
    if (rec.has_side_effects or rec.affected_workflows or rec.affected_schedules) and (
        not acknowledge_side_effects
    ):
        raise RollbackRecommendationError(
            "this capability has side effects and/or affects other workflows/schedules; "
            "rollback is never silent for these cases — review affected_workflows/"
            "affected_schedules/has_side_effects and set acknowledge_side_effects=True "
            "to proceed"
        )

    rollback_result = rollback_capability(
        catalogue_yaml, rec.capability_path, restore_version, reason, initiating_job_id,
        test_results, expected_current_version=expected_current_version, client=w,
    )

    if rec.required_tests:
        catalogue = load_catalogue(catalogue_yaml)
        manifest = load_test_manifests(manifest_yaml)
        verification = run_regression_tests(
            catalogue, manifest, rec.capability_path, executor=executor,
        )
        verification_passed = verification.passed
        verification_payload = verification.model_dump(mode="json")
    else:
        verification_passed = True
        verification_payload = None

    record = RollbackExecutionRecord(
        status="rollback_succeeded" if verification_passed else "verification_failed",
        capability_path=rec.capability_path, restore_version=restore_version, reason=reason,
        initiating_job_id=initiating_job_id, approved_by=approved_by,
        rollback=rollback_result, verification=verification_payload,
        verification_passed=verification_passed,
        details=None if verification_passed else (
            "rollback completed and the failed version remains in Windmill history, but "
            "post-rollback verification of impacted tests did not all pass; manual "
            "follow-up is required"
        ),
    )
    _persist_execution_record(w, rec.capability_path, initiating_job_id, record)
    return record.model_dump(mode="json")


def main(action: str, args_json: str) -> dict:
    args: dict[str, Any] = json.loads(args_json)
    if action == "recommend":
        return recommend_rollback(**args).model_dump(mode="json")
    if action == "execute":
        return execute_approved_rollback(**args)
    raise RollbackRecommendationError("action must be 'recommend' or 'execute'")
