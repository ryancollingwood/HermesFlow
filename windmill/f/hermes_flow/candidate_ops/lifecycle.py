"""HF-014 capability deprecation and history-preserving rollback."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import wmill
import yaml
from pydantic import BaseModel, Field

from f.hermes_flow.candidate_ops.diff import _consumer_impact, _get_script
from f.hermes_flow.candidate_ops.promote import (
    PromotionConflict,
    PromotionError,
    TestResult,
    WindmillPromotionClient,
    _validated_tests,
)
from f.hermes_flow.catalogue.models import Catalogue, load_catalogue
from f.libraries.capability.models import CapabilityMaturity


class DeprecationRecord(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    reason: str = Field(..., min_length=1)
    initiating_job_id: str = Field(..., min_length=1)
    affected_workflows: list[str]
    affected_schedules: list[dict]
    deprecated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RollbackRecord(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    failed_version: str
    restored_from_version: str
    rollback_version: str
    rollback_target: str
    reason: str = Field(..., min_length=1)
    initiating_job_id: str = Field(..., min_length=1)
    affected_workflows: list[str]
    affected_schedules: list[dict]
    rerun_tests: list[TestResult]
    rolled_back_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def lifecycle_variable_path(path: str, action: str, job_id: str) -> str:
    safe = path.replace("/", "_")
    return f"f/hermes_flow_state/{action}/{safe}_{job_id}"


def _schedules(client: WindmillPromotionClient, paths: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for path in paths:
        response = client.get(
            f"/w/{client.workspace}/schedules/list?path={quote(path, safe='')}&per_page=100",
            raise_for_status=False,
        )
        if response.status_code != 200:
            raise PromotionError(f"failed to inspect schedules for {path!r}: {response.status_code}")
        for schedule in response.json():
            key = schedule.get("path") or json.dumps(schedule, sort_keys=True)
            found[key] = schedule
    return [found[key] for key in sorted(found)]


def _impact(client: WindmillPromotionClient, catalogue: Catalogue, path: str) -> dict:
    consumers = _consumer_impact(catalogue, path)
    workflows = sorted(item["path"] for item in consumers)
    schedules = _schedules(client, [path, *workflows])
    entry = catalogue.get(path)
    tests = set(entry.metadata.test_requirements if entry else [])
    for item in consumers:
        tests.update(item["tests"])
    return {
        "workflows": workflows,
        "consumers": consumers,
        "schedules": schedules,
        "required_tests": sorted(tests),
    }


def deprecate_capability(
    catalogue_yaml: str,
    capability_path: str,
    reason: str,
    initiating_job_id: str,
    client: Optional[WindmillPromotionClient] = None,
) -> dict:
    w = client or wmill.Windmill()
    catalogue = load_catalogue(catalogue_yaml)
    entry = catalogue.get(capability_path)
    if entry is None:
        raise PromotionError(f"unknown capability {capability_path!r}")
    impact = _impact(w, catalogue, capability_path)
    entry.metadata.maturity = CapabilityMaturity.deprecated
    record = DeprecationRecord(
        capability_path=capability_path,
        reason=reason,
        initiating_job_id=initiating_job_id,
        affected_workflows=impact["workflows"],
        affected_schedules=impact["schedules"],
    )
    response = w.post(
        f"/w/{w.workspace}/variables/create",
        json={
            "path": lifecycle_variable_path(capability_path, "deprecations", initiating_job_id),
            "value": record.model_dump_json(),
            "is_secret": False,
            "description": f"HF-014 deprecation record for {capability_path}",
        },
        raise_for_status=False,
    )
    if response.status_code not in (200, 201):
        raise PromotionError(f"failed to persist deprecation record: {response.status_code}")
    updated = catalogue.model_dump(mode="json")
    return {
        "record": record.model_dump(mode="json"),
        "impact": impact,
        "updated_catalogue_yaml": yaml.safe_dump(updated, sort_keys=False),
    }


def rollback_capability(
    catalogue_yaml: str,
    capability_path: str,
    restore_version: str,
    reason: str,
    initiating_job_id: str,
    test_results: list[dict],
    expected_current_version: Optional[str] = None,
    client: Optional[WindmillPromotionClient] = None,
) -> dict:
    w = client or wmill.Windmill()
    catalogue = load_catalogue(catalogue_yaml)
    if catalogue.get(capability_path) is None:
        raise PromotionError(f"unknown capability {capability_path!r}")
    impact = _impact(w, catalogue, capability_path)
    tests = _validated_tests(
        impact["required_tests"], [TestResult.model_validate(item) for item in test_results]
    )
    active = _get_script(w, capability_path)
    current_hash = active.get("hash")
    if expected_current_version and current_hash != expected_current_version:
        raise PromotionConflict(
            f"active version changed: expected {expected_current_version}, found {current_hash}"
        )
    historical_response = w.get(
        f"/w/{w.workspace}/scripts/get/h/{restore_version}", raise_for_status=False
    )
    if historical_response.status_code != 200:
        raise PromotionError(f"rollback version {restore_version!r} was not found")
    historical = historical_response.json()
    payload = {
        "path": capability_path,
        "parent_hash": current_hash,
        "content": historical.get("content", ""),
        "language": historical.get("language", active.get("language", "python3")),
        "summary": historical.get("summary", active.get("summary", "")),
        "description": historical.get("description", active.get("description", "")),
        "schema": historical.get("schema", active.get("schema", {})),
        "deployment_message": (
            f"HF-014 rollback restore={restore_version} failed_version={current_hash} "
            f"reason={reason} initiating_job={initiating_job_id}"
        ),
    }
    response = w.post(f"/w/{w.workspace}/scripts/create", json=payload, raise_for_status=False)
    if response.status_code not in (200, 201):
        if response.status_code in (409, 422):
            raise PromotionConflict(f"concurrent rollback conflict: {response.text}")
        raise PromotionError(f"rollback write failed: {response.status_code} {response.text}")
    rolled_back = _get_script(w, capability_path)
    rollback_hash = rolled_back.get("hash")
    if not rollback_hash or rollback_hash == current_hash:
        raise PromotionError("rollback did not create a new active version")
    record = RollbackRecord(
        capability_path=capability_path,
        failed_version=current_hash,
        restored_from_version=restore_version,
        rollback_version=rollback_hash,
        rollback_target=restore_version,
        reason=reason,
        initiating_job_id=initiating_job_id,
        affected_workflows=impact["workflows"],
        affected_schedules=impact["schedules"],
        rerun_tests=tests,
    )
    provenance = w.post(
        f"/w/{w.workspace}/variables/create",
        json={
            "path": lifecycle_variable_path(capability_path, "rollbacks", initiating_job_id),
            "value": record.model_dump_json(),
            "is_secret": False,
            "description": f"HF-014 rollback record for {capability_path}",
        },
        raise_for_status=False,
    )
    if provenance.status_code not in (200, 201):
        raise PromotionError(f"rollback {rollback_hash} succeeded but provenance write failed")
    return {"record": record.model_dump(mode="json"), "impact": impact}


def main(action: str, args_json: str) -> dict:
    args: dict[str, Any] = json.loads(args_json)
    if action == "deprecate":
        return deprecate_capability(**args)
    if action == "rollback":
        return rollback_capability(**args)
    raise PromotionError("action must be 'deprecate' or 'rollback'")
