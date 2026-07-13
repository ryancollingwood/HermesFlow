"""HF-015 test manifest discovery and bounded Windmill runner."""
from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Optional, Protocol

import wmill
import yaml
from pydantic import BaseModel, Field, field_validator


class TestType(str, Enum):
    fixture = "fixture"
    contract = "contract"
    smoke = "smoke"
    live_integration = "live_integration"


class TestMode(str, Enum):
    promotion_gating = "promotion_gating"
    scheduled = "scheduled"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    skipped = "skipped"


class TestSpec(BaseModel):
    id: str = Field(..., min_length=1)
    capability_paths: list[str] = Field(..., min_length=1)
    type: TestType
    mode: TestMode
    script_path: str = Field(..., min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_data_bytes: int = Field(default=1_000_000, ge=1, le=100_000_000)
    skip_reason: Optional[str] = None


class TestManifest(BaseModel):
    schema_version: str = "1.0"
    tests: list[TestSpec]

    @field_validator("tests")
    @classmethod
    def _unique_ids(cls, value: list[TestSpec]) -> list[TestSpec]:
        ids = [test.id for test in value]
        duplicates = sorted({test_id for test_id in ids if ids.count(test_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate test id(s): {duplicates}")
        return value


class TestEvidence(BaseModel):
    test: str
    capability_paths: list[str]
    type: TestType
    mode: TestMode
    status: TestStatus
    job_id: Optional[str] = None
    duration_ms: int = 0
    details: Optional[str] = None
    data_bytes: int = 0


class TestRunResult(BaseModel):
    schema_version: str = "1.0"
    capability_path: str
    mode: Optional[TestMode] = None
    passed: bool
    evidence: list[TestEvidence]


class TestExecutor(Protocol):
    def run(self, spec: TestSpec) -> tuple[str, Any]: ...


class WindmillTestExecutor:
    def __init__(self, client=None):
        self.client = client or wmill.Windmill()

    def run(self, spec: TestSpec) -> tuple[str, Any]:
        job_id = self.client.run_script_by_path_async(spec.script_path, args=spec.args)
        try:
            result = self.client.wait_job(job_id, timeout=spec.timeout_seconds, cleanup=False)
        except TimeoutError:
            self.client.cancel_job(job_id, reason=f"HF-015 timeout after {spec.timeout_seconds}s")
            raise
        return job_id, result


def load_test_manifests(*manifest_yamls: str) -> TestManifest:
    tests: list[dict] = []
    schema_version = "1.0"
    for manifest_yaml in manifest_yamls:
        raw = yaml.safe_load(manifest_yaml) or {}
        if not isinstance(raw, dict) or not isinstance(raw.get("tests", []), list):
            raise ValueError("test manifest must be a mapping with a tests list")
        schema_version = raw.get("schema_version", schema_version)
        tests.extend(raw.get("tests", []))
    return TestManifest(schema_version=schema_version, tests=tests)


def discover_tests(
    manifest: TestManifest,
    capability_path: str,
    required_test_ids: list[str],
    mode: Optional[TestMode] = None,
) -> list[TestSpec]:
    by_id = {test.id: test for test in manifest.tests}
    missing = sorted(set(required_test_ids) - set(by_id))
    if missing:
        raise ValueError(f"required test id(s) not found in manifests: {missing}")
    selected = []
    for test_id in required_test_ids:
        test = by_id[test_id]
        if capability_path not in test.capability_paths:
            raise ValueError(f"test {test_id!r} does not declare capability {capability_path!r}")
        if mode is None or test.mode is mode:
            selected.append(test)
    return selected


def run_tests(
    manifest: TestManifest,
    capability_path: str,
    required_test_ids: list[str],
    mode: Optional[TestMode] = None,
    max_timeout_seconds: int = 300,
    max_data_bytes: int = 5_000_000,
    executor: Optional[TestExecutor] = None,
) -> TestRunResult:
    selected = discover_tests(manifest, capability_path, required_test_ids, mode)
    runner = executor or WindmillTestExecutor()
    evidence: list[TestEvidence] = []
    for test in selected:
        started = time.monotonic()
        if test.skip_reason:
            evidence.append(
                TestEvidence(
                    test=test.id, capability_paths=test.capability_paths, type=test.type,
                    mode=test.mode, status=TestStatus.skipped, details=test.skip_reason,
                )
            )
            continue
        if test.timeout_seconds > max_timeout_seconds or test.max_data_bytes > max_data_bytes:
            evidence.append(
                TestEvidence(
                    test=test.id, capability_paths=test.capability_paths, type=test.type,
                    mode=test.mode, status=TestStatus.failed,
                    details="test bounds exceed runner limits",
                )
            )
            continue
        args_size = len(json.dumps(test.args, default=str).encode())
        if args_size > min(test.max_data_bytes, max_data_bytes):
            evidence.append(
                TestEvidence(
                    test=test.id, capability_paths=test.capability_paths, type=test.type,
                    mode=test.mode, status=TestStatus.failed, data_bytes=args_size,
                    details="test input exceeds data limit",
                )
            )
            continue
        try:
            job_id, result = runner.run(test)
            data_size = len(json.dumps(result, default=str).encode())
            declared = result.get("status") if isinstance(result, dict) else None
            status = {
                "pass": TestStatus.passed,
                "passed": TestStatus.passed,
                "fail": TestStatus.failed,
                "failed": TestStatus.failed,
                "skip": TestStatus.skipped,
                "skipped": TestStatus.skipped,
            }.get(declared, TestStatus.passed)
            details = result.get("details") if isinstance(result, dict) else None
            if data_size > min(test.max_data_bytes, max_data_bytes):
                status = TestStatus.failed
                details = "test output exceeds data limit"
            evidence.append(
                TestEvidence(
                    test=test.id, capability_paths=test.capability_paths, type=test.type,
                    mode=test.mode, status=status, job_id=job_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details=details, data_bytes=data_size,
                )
            )
        except Exception as exc:
            evidence.append(
                TestEvidence(
                    test=test.id, capability_paths=test.capability_paths, type=test.type,
                    mode=test.mode, status=TestStatus.failed,
                    duration_ms=int((time.monotonic() - started) * 1000), details=str(exc),
                )
            )
    return TestRunResult(
        capability_path=capability_path,
        mode=mode,
        passed=bool(evidence) and all(item.status is not TestStatus.failed for item in evidence),
        evidence=evidence,
    )


def main(
    manifest_yaml: str,
    capability_path: str,
    required_test_ids: list[str],
    mode: str = "",
    max_timeout_seconds: int = 300,
    max_data_bytes: int = 5_000_000,
) -> dict:
    result = run_tests(
        load_test_manifests(manifest_yaml), capability_path, required_test_ids,
        mode=TestMode(mode) if mode else None,
        max_timeout_seconds=max_timeout_seconds, max_data_bytes=max_data_bytes,
    )
    return result.model_dump(mode="json")
