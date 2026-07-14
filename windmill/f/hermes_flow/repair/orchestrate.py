"""HF-032 bounded adaptive repair preparation.

Inspection, policy, generation, fixture promotion, affected-consumer tests, and
promotion preparation happen before the native Windmill approval suspension.
No active capability is changed by this module.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol
from urllib.parse import quote

import wmill
from pydantic import BaseModel, Field, model_validator

from f.hermes_flow.candidate_ops.promote import prepare_promotion
from f.hermes_flow.catalogue.models import load_catalogue
from f.hermes_flow.policies.evaluator import PolicyContext, PolicyDecision, PolicyOutcome, evaluate_policy
from f.hermes_flow.repair.generate_candidate import generate_repair_candidate
from f.hermes_flow.repair.inspection import inspect_failure_from_windmill
from f.hermes_flow.repair.models import FailureCategory, RepairContext
from f.hermes_flow.repair.promote_fixture import FixturePromotionError, promote_source_drift_fixture
from f.hermes_flow.testing.regression import RegressionRunResult, run_regression_tests
from f.hermes_flow.testing.runner import TestExecutor, TestStatus, load_test_manifests
from f.libraries.capability.models import AutonomyAction
from f.libraries.lineage.models import ArtifactRef, ExecutionContext
from f.libraries.storage.artifacts import FilesystemArtifactStore


CAPABILITY_PATH = "f/hermes_flow/repair/adaptive_repair"
CAPABILITY_VERSION = "1.0.0"
STATE_ROOT = "f/hermes_flow_state/adaptive_repair"
REPAIRABLE_CATEGORIES = {
    FailureCategory.source_drift,
    FailureCategory.code_defect,
    FailureCategory.dependency,
}


class AdaptiveRepairError(ValueError):
    pass


class AttemptLimitExceeded(AdaptiveRepairError):
    pass


class _Response(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class WindmillRepairClient(Protocol):
    workspace: str

    def get(self, path: str, raise_for_status: bool = True) -> _Response: ...
    def post(self, path: str, json: dict, raise_for_status: bool = True) -> _Response: ...


class AttemptRecord(BaseModel):
    schema_version: str = "1.0"
    failed_job_id: str
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1, le=3)
    status: str
    candidate_id: Optional[str] = None
    candidate_path: Optional[str] = None
    repair_trace_id: Optional[str] = None
    retry_job_id: Optional[str] = None
    details: Optional[str] = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RepairPreparation(BaseModel):
    schema_version: str = "1.0"
    status: Literal[
        "policy_denied", "generation_rejected", "fixture_rejected",
        "tests_failed", "ready_for_approval",
    ]
    failed_job_id: str
    source_path: str
    attempt: int
    max_attempts: int
    attempt_state_path: str
    original_context: ExecutionContext
    repair_context: RepairContext
    repair_policy: PolicyDecision
    candidate: Optional[dict] = None
    promoted_fixtures: list[dict] = Field(default_factory=list)
    regression: Optional[dict] = None
    promotion: Optional[dict] = None
    stop_reason: Optional[str] = None

    @model_validator(mode="after")
    def _ready_requires_complete_evidence(self) -> "RepairPreparation":
        if self.status == "ready_for_approval":
            missing = [
                name for name, value in (
                    ("candidate", self.candidate),
                    ("regression", self.regression),
                    ("promotion", self.promotion),
                ) if not value
            ]
            if missing:
                raise ValueError(
                    "ready_for_approval preparation is missing " + ", ".join(missing)
                )
        return self


def attempt_state_path(failed_job_id: str, attempt: int) -> str:
    key = hashlib.sha256(failed_job_id.encode()).hexdigest()[:16]
    return f"{STATE_ROOT}/{key}_attempt_{attempt}"


def _put_attempt(client: WindmillRepairClient, path: str, record: AttemptRecord) -> None:
    record.updated_at = datetime.now(timezone.utc)
    response = client.post(
        f"/w/{client.workspace}/variables/update/{quote(path, safe='/')}",
        json={"value": record.model_dump_json()},
        raise_for_status=False,
    )
    if response.status_code not in (200, 201):
        raise AdaptiveRepairError(f"failed to update attempt state: HTTP {response.status_code}")


def _reserve_attempt(
    client: WindmillRepairClient, failed_job_id: str, max_attempts: int
) -> tuple[str, AttemptRecord]:
    if not 1 <= max_attempts <= 3:
        raise AdaptiveRepairError("max_attempts must be between 1 and 3")
    for attempt in range(1, max_attempts + 1):
        path = attempt_state_path(failed_job_id, attempt)
        record = AttemptRecord(
            failed_job_id=failed_job_id,
            attempt=attempt,
            max_attempts=max_attempts,
            status="reserved",
        )
        response = client.post(
            f"/w/{client.workspace}/variables/create",
            json={
                "path": path,
                "value": record.model_dump_json(),
                "is_secret": False,
                "description": f"HF-032 bounded repair attempt for failed job {failed_job_id}",
            },
            raise_for_status=False,
        )
        if response.status_code in (200, 201):
            return path, record
        existing = client.get(
            f"/w/{client.workspace}/variables/get/{quote(path, safe='/')}",
            raise_for_status=False,
        )
        if existing.status_code != 200:
            raise AdaptiveRepairError(
                f"failed to reserve repair attempt {attempt}: HTTP {response.status_code}"
            )
    raise AttemptLimitExceeded(
        f"failed job {failed_job_id!r} has exhausted its {max_attempts} repair attempt(s)"
    )


def _finish(
    client: WindmillRepairClient,
    path: str,
    record: AttemptRecord,
    preparation: RepairPreparation,
) -> dict:
    record.status = preparation.status
    record.details = preparation.stop_reason
    if preparation.candidate:
        record.candidate_id = preparation.candidate.get("candidate_id")
        record.candidate_path = preparation.candidate.get("path")
        record.repair_trace_id = preparation.candidate.get("generation_trace_id")
    _put_attempt(client, path, record)
    return preparation.model_dump(mode="json")


def prepare_adaptive_repair(
    conn: dict,
    failed_job_id: str,
    catalogue_yaml: str,
    manifest_yaml: str,
    original_context: dict | ExecutionContext,
    *,
    max_attempts: int = 2,
    recent_test_evidence: Optional[list[dict]] = None,
    source_artifact: Optional[dict | ArtifactRef] = None,
    expected_behavior: Optional[dict] = None,
    fixture_binding: Optional[dict] = None,
    sanitization_rules: Optional[dict] = None,
    client: Optional[WindmillRepairClient] = None,
    hermes_client=None,
    test_executor: Optional[TestExecutor] = None,
    store: Optional[FilesystemArtifactStore] = None,
) -> dict:
    windmill = client or wmill.Windmill()
    parent = (
        original_context
        if isinstance(original_context, ExecutionContext)
        else ExecutionContext.model_validate(original_context)
    )
    path, attempt = _reserve_attempt(windmill, failed_job_id, max_attempts)
    context = inspect_failure_from_windmill(
        failed_job_id,
        catalogue_yaml,
        recent_test_evidence=recent_test_evidence,
        client=windmill,
    )
    entry = load_catalogue(catalogue_yaml).get(context.active_capability.path)
    modify_policy = evaluate_policy(
        PolicyContext(
            action=AutonomyAction.modify_candidate,
            capability=entry.metadata if entry else None,
        )
    )
    category_allowed = context.classification.category in REPAIRABLE_CATEGORIES
    if not category_allowed or modify_policy.outcome is PolicyOutcome.denied:
        reason = (
            f"repair policy denies generation for {context.classification.category.value} failures"
            if not category_allowed else modify_policy.reason
        )
        return _finish(windmill, path, attempt, RepairPreparation(
            status="policy_denied", failed_job_id=failed_job_id,
            source_path=context.active_capability.path, attempt=attempt.attempt,
            max_attempts=max_attempts, attempt_state_path=path,
            original_context=parent, repair_context=context, repair_policy=modify_policy,
            stop_reason=reason,
        ))

    generated = generate_repair_candidate(
        conn, context, catalogue_yaml, candidate_client=windmill,
        hermes_client=hermes_client, store=store,
    )
    candidate = dict(generated.candidate) if generated.candidate else None
    if generated.status != "candidate_created" or candidate is None:
        return _finish(windmill, path, attempt, RepairPreparation(
            status="generation_rejected", failed_job_id=failed_job_id,
            source_path=context.active_capability.path, attempt=attempt.attempt,
            max_attempts=max_attempts, attempt_state_path=path,
            original_context=parent, repair_context=context, repair_policy=modify_policy,
            stop_reason=generated.rejection_reason or "repair generation was rejected",
        ))

    fixtures: list[dict] = []
    if context.classification.category is FailureCategory.source_drift:
        if source_artifact is None or expected_behavior is None:
            preparation = RepairPreparation(
                status="fixture_rejected", failed_job_id=failed_job_id,
                source_path=context.active_capability.path, attempt=attempt.attempt,
                max_attempts=max_attempts, attempt_state_path=path,
                original_context=parent, repair_context=context, repair_policy=modify_policy,
                candidate=candidate,
                stop_reason="source-drift repair requires a retained source artifact and expected behavior",
            )
            return _finish(windmill, path, attempt, preparation)
        try:
            fixture = promote_source_drift_fixture(
                source_artifact, failed_job_id, context.active_capability.path,
                expected_behavior, binding=fixture_binding,
                sanitization_rules=sanitization_rules, store=store,
            )
        except (FixturePromotionError, ValueError) as exc:
            return _finish(windmill, path, attempt, RepairPreparation(
                status="fixture_rejected", failed_job_id=failed_job_id,
                source_path=context.active_capability.path, attempt=attempt.attempt,
                max_attempts=max_attempts, attempt_state_path=path,
                original_context=parent, repair_context=context, repair_policy=modify_policy,
                candidate=candidate, stop_reason=str(exc),
            ))
        fixtures.append(fixture.model_dump(mode="json"))

    regression: RegressionRunResult = run_regression_tests(
        load_catalogue(catalogue_yaml), load_test_manifests(manifest_yaml),
        context.active_capability.path, executor=test_executor,
        promoted_fixtures=fixtures, candidate_path=candidate["path"],
    )
    tests_passed = bool(regression.evidence) and all(
        item.status is TestStatus.passed for item in regression.evidence
    )
    if not tests_passed:
        return _finish(windmill, path, attempt, RepairPreparation(
            status="tests_failed", failed_job_id=failed_job_id,
            source_path=context.active_capability.path, attempt=attempt.attempt,
            max_attempts=max_attempts, attempt_state_path=path,
            original_context=parent, repair_context=context, repair_policy=modify_policy,
            candidate=candidate, promoted_fixtures=fixtures,
            regression=regression.model_dump(mode="json"),
            stop_reason="required candidate or affected-consumer regression tests did not all pass",
        ))

    test_results = [{
        "test": item.test,
        "passed": item.status is TestStatus.passed,
        "job_id": item.job_id,
        "details": item.details,
    } for item in regression.evidence]
    promotion = prepare_promotion(
        candidate["candidate_id"], catalogue_yaml, test_results, client=windmill
    )
    return _finish(windmill, path, attempt, RepairPreparation(
        status="ready_for_approval", failed_job_id=failed_job_id,
        source_path=context.active_capability.path, attempt=attempt.attempt,
        max_attempts=max_attempts, attempt_state_path=path,
        original_context=parent, repair_context=context, repair_policy=modify_policy,
        candidate=candidate, promoted_fixtures=fixtures,
        regression=regression.model_dump(mode="json"), promotion=promotion,
    ))


def main(
    conn: dict,
    failed_job_id: str,
    catalogue_yaml: str,
    manifest_yaml: str,
    original_context: dict,
    max_attempts: int = 2,
    recent_test_evidence: Optional[list[dict]] = None,
    source_artifact: Optional[dict] = None,
    expected_behavior: Optional[dict] = None,
    fixture_binding: Optional[dict] = None,
    sanitization_rules: Optional[dict] = None,
) -> dict:
    return prepare_adaptive_repair(
        conn, failed_job_id, catalogue_yaml, manifest_yaml, original_context,
        max_attempts=max_attempts, recent_test_evidence=recent_test_evidence,
        source_artifact=source_artifact, expected_behavior=expected_behavior,
        fixture_binding=fixture_binding, sanitization_rules=sanitization_rules,
    )
