"""HF-032 approved promotion and one bounded retry of the original execution."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional
from urllib.parse import quote

import wmill
from pydantic import BaseModel, Field

from f.hermes_flow.candidate_ops.promote import (
    PromotionConflict,
    PromotionError,
    finalize_promotion,
)
from f.hermes_flow.repair.orchestrate import (
    AttemptRecord,
    RepairPreparation,
    _put_attempt,
)
from f.libraries.lineage.models import ExecutionContext


CAPABILITY_PATH = "f/hermes_flow/repair/finalize_retry"
CAPABILITY_VERSION = "1.0.0"


class RetryRecord(BaseModel):
    schema_version: str = "1.0"
    status: Literal[
        "approval_rejected", "stale_conflict", "promotion_failed",
        "retry_succeeded", "retry_failed",
    ]
    failed_job_id: str
    source_path: str
    candidate_id: str = Field(min_length=1)
    candidate_path: str = Field(min_length=1)
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1, le=3)
    parent_trace_id: str
    retry_trace_id: Optional[str] = None
    promoted_version: Optional[str] = None
    retry_job_id: Optional[str] = None
    retry_result_sha256: Optional[str] = None
    approved_by: Optional[str] = None
    details: Optional[str] = None


def _record_from_preparation(prepared: RepairPreparation, status: str, **updates) -> RetryRecord:
    candidate = prepared.candidate or {}
    return RetryRecord(
        status=status,
        failed_job_id=prepared.failed_job_id,
        source_path=prepared.source_path,
        candidate_id=str(candidate.get("candidate_id") or ""),
        candidate_path=str(candidate.get("path") or ""),
        attempt=prepared.attempt,
        max_attempts=prepared.max_attempts,
        parent_trace_id=str(prepared.original_context.trace_id),
        **updates,
    )


def _persist(client, prepared: RepairPreparation, result: RetryRecord) -> dict:
    attempt = AttemptRecord(
        failed_job_id=prepared.failed_job_id,
        attempt=prepared.attempt,
        max_attempts=prepared.max_attempts,
        status=result.status,
        candidate_id=result.candidate_id,
        candidate_path=result.candidate_path,
        repair_trace_id=(prepared.candidate or {}).get("generation_trace_id"),
        retry_job_id=result.retry_job_id,
        details=result.details,
    )
    _put_attempt(client, prepared.attempt_state_path, attempt)
    return result.model_dump(mode="json")


def finalize_and_retry(
    prepared: dict | RepairPreparation,
    approval_granted: bool,
    approved_by: Optional[str] = None,
    *,
    context_argument: str = "context",
    retry_timeout_seconds: int = 300,
    client=None,
) -> dict:
    windmill = client or wmill.Windmill()
    preparation = (
        prepared if isinstance(prepared, RepairPreparation)
        else RepairPreparation.model_validate(prepared)
    )
    if preparation.status != "ready_for_approval" or not preparation.promotion:
        raise PromotionError("repair preparation is not ready for promotion approval")
    if not approval_granted:
        return _persist(windmill, preparation, _record_from_preparation(
            preparation, "approval_rejected", approved_by=approved_by,
            details="promotion approval was rejected; active version and original job were not changed",
        ))
    if not approved_by:
        raise PromotionError("promotion approval is missing an authenticated approver identity")

    try:
        promotion = finalize_promotion(
            preparation.promotion,
            approval_granted=True,
            approved_by=approved_by,
            client=windmill,
        )
    except PromotionConflict as exc:
        return _persist(windmill, preparation, _record_from_preparation(
            preparation, "stale_conflict", approved_by=approved_by, details=str(exc),
        ))
    except PromotionError as exc:
        return _persist(windmill, preparation, _record_from_preparation(
            preparation, "promotion_failed", approved_by=approved_by, details=str(exc),
        ))

    response = windmill.get(
        f"/w/{windmill.workspace}/jobs_u/get/{quote(preparation.failed_job_id, safe='')}",
        raise_for_status=False,
    )
    if response.status_code != 200:
        return _persist(windmill, preparation, _record_from_preparation(
            preparation, "retry_failed", approved_by=approved_by,
            promoted_version=promotion["promoted_version"],
            details=f"original failed job could not be reloaded: HTTP {response.status_code}",
        ))
    job = response.json()
    original_path = str(job.get("script_path") or job.get("path") or "")
    if original_path != promotion["active_path"]:
        return _persist(windmill, preparation, _record_from_preparation(
            preparation, "retry_failed", approved_by=approved_by,
            promoted_version=promotion["promoted_version"],
            details="original job path does not match the promoted active capability",
        ))
    original_args = job.get("args")
    if not isinstance(original_args, dict):
        return _persist(windmill, preparation, _record_from_preparation(
            preparation, "retry_failed", approved_by=approved_by,
            promoted_version=promotion["promoted_version"],
            details="original job arguments were unavailable",
        ))
    supplied_context = original_args.get(context_argument)
    if isinstance(supplied_context, dict):
        try:
            original_job_context = ExecutionContext.model_validate(supplied_context)
        except ValueError:
            original_job_context = None
        if (
            original_job_context is not None
            and original_job_context.trace_id != preparation.original_context.trace_id
        ):
            return _persist(windmill, preparation, _record_from_preparation(
                preparation, "retry_failed", approved_by=approved_by,
                promoted_version=promotion["promoted_version"],
                details="original job lineage does not match the approved repair parent trace",
            ))

    retry_context = ExecutionContext(
        parent_trace_id=preparation.original_context.trace_id,
        conversation_id=preparation.original_context.conversation_id,
        request_id=preparation.failed_job_id,
        capability=promotion["active_path"],
        capability_version=promotion["promoted_version"],
        initiating_actor="adaptive-repair",
    )
    retry_args = dict(original_args)
    retry_args[context_argument] = retry_context.model_dump(mode="json")
    retry_job_id = windmill.run_script_by_path_async(promotion["active_path"], args=retry_args)
    try:
        retry_result: Any = windmill.wait_job(
            retry_job_id, timeout=retry_timeout_seconds, cleanup=False
        )
        digest = hashlib.sha256(
            json.dumps(retry_result, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        result = _record_from_preparation(
            preparation, "retry_succeeded", approved_by=approved_by,
            promoted_version=promotion["promoted_version"], retry_job_id=retry_job_id,
            retry_trace_id=str(retry_context.trace_id), retry_result_sha256=digest,
        )
    except Exception as exc:
        result = _record_from_preparation(
            preparation, "retry_failed", approved_by=approved_by,
            promoted_version=promotion["promoted_version"], retry_job_id=retry_job_id,
            retry_trace_id=str(retry_context.trace_id),
            details=f"{type(exc).__name__}: retry failed; inspect the linked retry job logs",
        )
    return _persist(windmill, preparation, result)


def main(
    prepared_json: str,
    approval_granted: bool,
    approved_by: str = "",
    context_argument: str = "context",
    retry_timeout_seconds: int = 300,
) -> dict:
    return finalize_and_retry(
        json.loads(prepared_json), approval_granted, approved_by or None,
        context_argument=context_argument, retry_timeout_seconds=retry_timeout_seconds,
    )
