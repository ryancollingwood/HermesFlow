"""HF-015 manifest discovery and structured-runner tests."""
import pathlib

import pytest
from f.hermes_flow.testing.runner import (
    ExecutionAssetKind,
    WindmillTestExecutor,
    discover_tests,
    load_test_manifests,
    run_tests,
)
from f.hermes_flow.testing.runner import (
    TestMode as RunnerMode,
)
from f.hermes_flow.testing.runner import (
    TestStatus as RunnerStatus,
)

MANIFEST = """
schema_version: '1.0'
tests:
  - id: contract/base
    capability_paths: [f/capabilities/base]
    type: contract
    mode: promotion_gating
    script_path: f/tests/pass
    timeout_seconds: 10
    max_data_bytes: 1000
  - id: smoke/base
    capability_paths: [f/capabilities/base]
    type: smoke
    mode: scheduled
    script_path: f/tests/fail
    timeout_seconds: 10
    max_data_bytes: 1000
  - id: fixture/skipped
    capability_paths: [f/capabilities/base]
    type: fixture
    mode: promotion_gating
    script_path: f/tests/skip
    skip_reason: optional dependency unavailable
"""


class FakeExecutor:
    def __init__(self, results=None):
        self.results = results or {}
        self.ran = []

    def run(self, spec):
        self.ran.append(spec.id)
        result = self.results.get(spec.id, {"status": "pass", "details": "ok"})
        if isinstance(result, Exception):
            raise result
        return f"job-{spec.id}", result


def test_discovery_resolves_metadata_ids_and_preserves_order():
    manifest = load_test_manifests(MANIFEST)
    found = discover_tests(
        manifest, "f/capabilities/base", ["fixture/skipped", "contract/base"]
    )
    assert [test.id for test in found] == ["fixture/skipped", "contract/base"]


def test_discovery_fails_on_unknown_metadata_id():
    with pytest.raises(ValueError, match="not found"):
        discover_tests(load_test_manifests(MANIFEST), "f/capabilities/base", ["missing"])


def test_promotion_and_scheduled_tests_are_distinguishable():
    manifest = load_test_manifests(MANIFEST)
    gating = discover_tests(
        manifest, "f/capabilities/base", ["contract/base", "smoke/base"],
        RunnerMode.promotion_gating,
    )
    scheduled = discover_tests(
        manifest, "f/capabilities/base", ["contract/base", "smoke/base"],
        RunnerMode.scheduled,
    )
    assert [test.id for test in gating] == ["contract/base"]
    assert [test.id for test in scheduled] == ["smoke/base"]


def test_runner_returns_passing_failing_and_skipped_evidence():
    executor = FakeExecutor({"smoke/base": {"status": "fail", "details": "bad response"}})
    result = run_tests(
        load_test_manifests(MANIFEST), "f/capabilities/base",
        ["contract/base", "smoke/base", "fixture/skipped"], executor=executor,
    )
    assert result.passed is False
    assert [item.status for item in result.evidence] == [
        RunnerStatus.passed, RunnerStatus.failed, RunnerStatus.skipped
    ]
    assert result.evidence[0].job_id == "job-contract/base"
    assert executor.ran == ["contract/base", "smoke/base"]


def test_runner_rejects_test_bounds_above_global_limits_without_execution():
    executor = FakeExecutor()
    result = run_tests(
        load_test_manifests(MANIFEST), "f/capabilities/base", ["contract/base"],
        max_timeout_seconds=5, executor=executor,
    )
    assert result.evidence[0].status is RunnerStatus.failed
    assert "bounds exceed" in result.evidence[0].details
    assert executor.ran == []


def test_runner_fails_oversized_output():
    executor = FakeExecutor({"contract/base": {"status": "pass", "blob": "x" * 2000}})
    result = run_tests(
        load_test_manifests(MANIFEST), "f/capabilities/base", ["contract/base"],
        executor=executor,
    )
    assert result.evidence[0].status is RunnerStatus.failed
    assert result.evidence[0].details == "test output exceeds data limit"


def test_executor_exception_becomes_structured_failure():
    executor = FakeExecutor({"contract/base": TimeoutError("timed out")})
    result = run_tests(
        load_test_manifests(MANIFEST), "f/capabilities/base", ["contract/base"],
        executor=executor,
    )
    assert result.evidence[0].status is RunnerStatus.failed
    assert result.evidence[0].details == "timed out"


def test_duplicate_ids_across_manifests_are_rejected():
    with pytest.raises(ValueError, match="duplicate test id"):
        load_test_manifests(MANIFEST, MANIFEST)


def test_windmill_executor_submits_flow_assets_as_flows():
    class Client:
        def __init__(self):
            self.calls = []

        def run_flow_async(self, path, args):
            self.calls.append(("flow", path, args))
            return "flow-job"

        def wait_job(self, job_id, timeout, cleanup):
            return {"status": "pass", "job_id": job_id}

    client = Client()
    flow_manifest = load_test_manifests("""
tests:
  - id: smoke/flow
    capability_paths: [f/workflows/example]
    type: smoke
    mode: promotion_gating
    script_path: f/workflows/example
    asset_kind: flow
    args: {bounded: true}
""")
    job_id, result = WindmillTestExecutor(client).run(flow_manifest.tests[0])
    assert client.calls == [("flow", "f/workflows/example", {"bounded": True})]
    assert job_id == "flow-job"
    assert result["status"] == "pass"


def test_checked_in_manifests_are_discoverable_and_reference_real_scripts():
    tests_root = pathlib.Path(__file__).parent
    files = sorted(tests_root.glob("**/*.test.yaml"))
    manifest = load_test_manifests(*(file.read_text() for file in files))
    assert {test.type.value for test in manifest.tests} == {
        "fixture", "contract", "smoke", "live_integration"
    }
    windmill_root = tests_root.parent
    for test in manifest.tests:
        if test.asset_kind is ExecutionAssetKind.flow:
            path = windmill_root / f"{test.script_path}.flow" / "flow.yaml"
        else:
            path = windmill_root / f"{test.script_path}.py"
        assert path.exists(), test.script_path
