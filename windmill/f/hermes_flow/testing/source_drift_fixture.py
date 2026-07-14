"""HF-031 generic runner for a promoted source-drift regression fixture."""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

import wmill
from pydantic import BaseModel, ConfigDict, Field

from f.hermes_flow.candidate_ops.models import is_candidate_path
from f.hermes_flow.repair.promote_fixture import SourceDriftFixture
from f.libraries.storage.artifacts import FilesystemArtifactStore


CAPABILITY_PATH = "f/hermes_flow/testing/source_drift_fixture"
CAPABILITY_VERSION = "1.0.0"


class FixtureAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    passed: bool
    expectation: str
    actual: Optional[Any] = None


class SourceDriftFixtureRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    status: Literal["pass", "fail"]
    fixture_id: str
    failed_job_id: str
    candidate_path: str
    candidate_job_id: Optional[str] = None
    assertions: list[FixtureAssertion] = Field(default_factory=list)
    details: str


def _resolve_path(value: Any, path: str) -> tuple[bool, Any]:
    if path in {"", "$"}:
        return True, value
    current = value
    for part in path.removeprefix("$.").split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _assert_expected(result: Any, fixture: SourceDriftFixture) -> list[FixtureAssertion]:
    expected = fixture.expected_behavior
    assertions = []
    for path in expected.required_paths:
        found, actual = _resolve_path(result, path)
        assertions.append(
            FixtureAssertion(
                path=path, passed=found, expectation="path exists", actual=actual
            )
        )
    for path, wanted in expected.expected_values.items():
        found, actual = _resolve_path(result, path)
        assertions.append(
            FixtureAssertion(
                path=path,
                passed=found and actual == wanted,
                expectation=f"equals {wanted!r}",
                actual=actual,
            )
        )
    for path, minimum in expected.minimum_item_counts.items():
        found, actual = _resolve_path(result, path)
        count = len(actual) if found and hasattr(actual, "__len__") else None
        assertions.append(
            FixtureAssertion(
                path=path,
                passed=count is not None and count >= minimum,
                expectation=f"contains at least {minimum} items",
                actual=count,
            )
        )
    return assertions


def run_source_drift_fixture(
    fixture_record: SourceDriftFixture | dict,
    candidate_path: str,
    *,
    client=None,
    store: Optional[FilesystemArtifactStore] = None,
) -> SourceDriftFixtureRunResult:
    if not is_candidate_path(candidate_path):
        raise ValueError("source-drift fixtures may execute only isolated candidate paths")
    fixture = (
        fixture_record
        if isinstance(fixture_record, SourceDriftFixture)
        else SourceDriftFixture.model_validate(fixture_record)
    )
    artifact_store = store or FilesystemArtifactStore()
    raw = artifact_store.read(fixture.fixture_artifact)
    if len(raw) > fixture.binding.max_data_bytes:
        return SourceDriftFixtureRunResult(
            status="fail",
            fixture_id=fixture.fixture_id,
            failed_job_id=fixture.failed_job_id,
            candidate_path=candidate_path,
            details="fixture exceeds declared candidate data bound",
        )
    if fixture.binding.payload_mode == "json":
        payload: Any = json.loads(raw)
    elif fixture.binding.payload_mode == "artifact_ref":
        payload = fixture.fixture_artifact.model_dump(mode="json")
    else:
        payload = raw.decode("utf-8")
    args = dict(fixture.binding.candidate_args)
    args[fixture.binding.fixture_argument] = payload
    windmill = client or wmill.Windmill()
    job_id = windmill.run_script_by_path_async(candidate_path, args=args)
    try:
        result = windmill.wait_job(
            job_id, timeout=fixture.binding.timeout_seconds, cleanup=False
        )
    except TimeoutError:
        windmill.cancel_job(
            job_id,
            reason=f"HF-031 fixture timeout after {fixture.binding.timeout_seconds}s",
        )
        raise
    assertions = _assert_expected(result, fixture)
    passed = all(assertion.passed for assertion in assertions)
    return SourceDriftFixtureRunResult(
        status="pass" if passed else "fail",
        fixture_id=fixture.fixture_id,
        failed_job_id=fixture.failed_job_id,
        candidate_path=candidate_path,
        candidate_job_id=job_id,
        assertions=assertions,
        details=(
            fixture.expected_behavior.description
            if passed
            else "candidate output did not satisfy promoted fixture expectations"
        ),
    )


def main(fixture_record: dict, candidate_path: str) -> dict:
    return run_source_drift_fixture(fixture_record, candidate_path).model_dump(mode="json")
