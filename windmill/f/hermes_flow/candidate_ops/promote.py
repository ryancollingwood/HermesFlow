"""HF-013 candidate promotion safety core and Windmill entrypoints.

``prepare_promotion`` builds and validates the evidence before a Windmill flow
suspends for approval. ``finalize_promotion`` is the only write step and repeats
the base-version check immediately before its optimistic versioned write.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

import wmill
from f.hermes_flow.candidate_ops.diff import _get_script, analyse_candidate
from f.hermes_flow.candidate_ops.models import CandidateRecord, metadata_variable_path
from f.hermes_flow.catalogue.models import load_catalogue
from f.hermes_flow.policies.evaluator import (
    PolicyContext,
    PolicyDecision,
    PolicyOutcome,
    evaluate_policy,
)
from f.libraries.capability.models import AutonomyAction
from pydantic import BaseModel, Field


class PromotionError(ValueError):
    pass


class PromotionConflict(PromotionError):
    pass


class _Response(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class WindmillPromotionClient(Protocol):
    workspace: str

    def get(self, path: str, raise_for_status: bool = True) -> _Response: ...
    def post(self, path: str, json: dict, raise_for_status: bool = True) -> _Response: ...


class TestResult(BaseModel):
    test: str = Field(..., min_length=1)
    passed: bool
    job_id: str | None = None
    details: str | None = None


class PromotionRecord(BaseModel):
    schema_version: str = "1.0"
    candidate_id: str
    candidate_path: str
    active_path: str
    base_version: str
    promoted_version: str
    rollback_target: str
    approved_by: str | None = None
    policy: dict
    required_tests: list[TestResult]
    promoted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def promotion_variable_path(candidate_id: str) -> str:
    return f"f/hermes_flow/candidates/{candidate_id}_promotion"


def _candidate_record(client: WindmillPromotionClient, candidate_id: str) -> CandidateRecord:
    response = client.get(
        f"/w/{client.workspace}/variables/get/{metadata_variable_path(candidate_id)}",
        raise_for_status=False,
    )
    if response.status_code != 200:
        raise PromotionError(f"candidate metadata for {candidate_id!r} was not found")
    return CandidateRecord.model_validate_json(response.json()["value"])


def _validated_tests(required: list[str], supplied: list[TestResult]) -> list[TestResult]:
    by_name = {result.test: result for result in supplied}
    missing = sorted(set(required) - set(by_name))
    failed = sorted(name for name in required if name in by_name and not by_name[name].passed)
    if missing or failed:
        parts = []
        if missing:
            parts.append(f"missing required tests: {missing}")
        if failed:
            parts.append(f"failed required tests: {failed}")
        raise PromotionError("; ".join(parts))
    return [by_name[name] for name in required]


def prepare_promotion(
    candidate_id: str,
    catalogue_yaml: str,
    test_results: list[dict],
    candidate_capability_metadata: dict | None = None,
    client: WindmillPromotionClient | None = None,
) -> dict:
    w = client or wmill.Windmill()
    record = _candidate_record(w, candidate_id)
    if not record.source_path or not record.base_version:
        raise PromotionError("only candidates derived from an active source can be promoted")
    catalogue = load_catalogue(catalogue_yaml)
    entry = catalogue.get(record.source_path)
    decision = evaluate_policy(
        PolicyContext(
            action=AutonomyAction.promote,
            capability=entry.metadata if entry else None,
        )
    )
    if decision.outcome is PolicyOutcome.denied:
        raise PromotionError(f"promotion denied by policy: {decision.reason}")

    evidence = analyse_candidate(
        candidate=_get_script(w, record.path),
        active=_get_script(w, record.source_path),
        candidate_path=record.path,
        active_path=record.source_path,
        catalogue=catalogue,
        candidate_capability_metadata=candidate_capability_metadata,
    )
    if evidence["no_changes"]:
        raise PromotionError("candidate has no changes to promote")
    tests = _validated_tests(
        evidence["promotion_summary"]["required_tests"],
        [TestResult.model_validate(result) for result in test_results],
    )
    return {
        "candidate": record.model_dump(mode="json"),
        "policy": decision.model_dump(mode="json"),
        "evidence": evidence,
        "tests": [result.model_dump(mode="json") for result in tests],
    }


def finalize_promotion(
    prepared: dict,
    approval_granted: bool,
    approved_by: str | None = None,
    client: WindmillPromotionClient | None = None,
) -> dict:
    w = client or wmill.Windmill()
    record = CandidateRecord.model_validate(prepared["candidate"])
    decision = PolicyDecision.model_validate(prepared["policy"])
    if decision.outcome is PolicyOutcome.denied:
        raise PromotionError("promotion denied by policy")
    if decision.outcome is PolicyOutcome.approval_required:
        if not approval_granted:
            raise PromotionError("promotion requires approval and was not approved")
        if not approved_by:
            raise PromotionError("promotion approval is missing an authenticated approver identity")
    if not record.source_path or not record.base_version:
        raise PromotionError("promotion record lacks active-source provenance")

    active = _get_script(w, record.source_path)
    current_hash = active.get("hash")
    if current_hash != record.base_version:
        raise PromotionConflict(
            f"active version changed since candidate creation: expected {record.base_version}, "
            f"found {current_hash}; refusing to overwrite"
        )
    candidate = _get_script(w, record.path)
    deployment_message = (
        f"HF-013 promote candidate={record.candidate_id} candidate_path={record.path} "
        f"base_version={record.base_version} rollback_target={current_hash}"
    )
    payload = {
        "path": record.source_path,
        "parent_hash": current_hash,
        "content": candidate.get("content", ""),
        "language": candidate.get("language", active.get("language", "python3")),
        "summary": candidate.get("summary", active.get("summary", "")),
        "description": candidate.get("description", active.get("description", "")),
        "schema": candidate.get("schema", active.get("schema", {})),
        "deployment_message": deployment_message,
    }
    response = w.post(
        f"/w/{w.workspace}/scripts/create", json=payload, raise_for_status=False
    )
    if response.status_code not in (200, 201):
        if response.status_code in (409, 422):
            raise PromotionConflict(f"concurrent version conflict: {response.text}")
        raise PromotionError(f"failed to promote candidate: {response.status_code} {response.text}")

    promoted = _get_script(w, record.source_path)
    promoted_hash = promoted.get("hash")
    if not promoted_hash or promoted_hash == current_hash:
        raise PromotionError("active script update did not produce a new version")
    promotion = PromotionRecord(
        candidate_id=record.candidate_id,
        candidate_path=record.path,
        active_path=record.source_path,
        base_version=record.base_version,
        promoted_version=promoted_hash,
        rollback_target=current_hash,
        approved_by=approved_by,
        policy=decision.model_dump(mode="json"),
        required_tests=[TestResult.model_validate(item) for item in prepared["tests"]],
    )
    provenance = w.post(
        f"/w/{w.workspace}/variables/create",
        json={
            "path": promotion_variable_path(record.candidate_id),
            "value": promotion.model_dump_json(),
            "is_secret": False,
            "description": f"HF-013 promotion provenance for {record.path}",
        },
        raise_for_status=False,
    )
    if provenance.status_code not in (200, 201):
        raise PromotionError(
            f"active version {promoted_hash} was created but provenance persistence failed: "
            f"{provenance.status_code} {provenance.text}"
        )
    return promotion.model_dump(mode="json")


def main(
    prepared_json: str,
    approval_granted: bool,
    approved_by: str = "",
) -> dict:
    return finalize_promotion(
        json.loads(prepared_json), approval_granted=approval_granted, approved_by=approved_by or None
    )
