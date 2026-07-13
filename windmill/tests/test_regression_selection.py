"""HF-016 dependency-aware regression selection tests."""
from f.hermes_flow.catalogue.models import CapabilityKind, Catalogue, CatalogueEntry
from f.hermes_flow.testing.regression import run_regression_tests, select_regression_tests
from f.hermes_flow.testing.runner import TestManifest as RunnerManifest
from f.hermes_flow.testing.runner import TestSpec as RunnerSpec
from f.libraries.capability.models import CapabilityMetadata, CapabilityMaturity


def entry(path, dependencies=(), tests=(), kind=CapabilityKind.script):
    return CatalogueEntry(
        kind=kind, tags=[], inputs_summary="input", outputs_summary="output",
        metadata=CapabilityMetadata(
            path=path, capability_version="1.0.0", summary=path,
            maturity=CapabilityMaturity.stable, owners=["platform"],
            dependencies=list(dependencies), test_requirements=list(tests),
        ),
    )


def spec(test_id, path, test_type="contract"):
    return RunnerSpec(
        id=test_id, capability_paths=[path], type=test_type,
        mode="promotion_gating", script_path=f"f/tests/{test_id}",
    )


class Executor:
    def __init__(self): self.ran = []
    def run(self, test):
        self.ran.append(test.id)
        return f"job-{test.id}", {"status": "pass"}


def test_changed_capability_tests_always_run_regardless_of_type():
    catalogue = Catalogue(entries=[entry("base", tests=["base-fixture", "base-live"])])
    manifest = RunnerManifest(tests=[
        spec("base-fixture", "base", "fixture"),
        spec("base-live", "base", "live_integration"),
    ])
    selected = select_regression_tests(catalogue, manifest, "base")
    assert [item.test.id for item in selected] == ["base-fixture", "base-live"]
    assert all(item.reasons[0].relationship == "changed" for item in selected)


def test_chain_selects_direct_and_transitive_consumer_contract_and_smoke_tests():
    catalogue = Catalogue(entries=[
        entry("base", tests=["base"]),
        entry("direct", ["base"], ["direct-contract"]),
        entry("transitive", ["direct"], ["transitive-smoke"], CapabilityKind.flow),
    ])
    manifest = RunnerManifest(tests=[
        spec("base", "base", "fixture"),
        spec("direct-contract", "direct", "contract"),
        spec("transitive-smoke", "transitive", "smoke"),
    ])
    selected = select_regression_tests(catalogue, manifest, "base")
    by_id = {item.test.id: item for item in selected}
    assert by_id["direct-contract"].reasons[0].relationship == "direct"
    assert by_id["transitive-smoke"].reasons[0].relationship == "transitive"
    assert by_id["transitive-smoke"].reasons[0].distance == 2


def test_consumer_fixture_and_live_tests_are_not_selected():
    catalogue = Catalogue(entries=[
        entry("base"),
        entry("consumer", ["base"], ["fixture", "live"]),
    ])
    manifest = RunnerManifest(tests=[
        spec("fixture", "consumer", "fixture"),
        spec("live", "consumer", "live_integration"),
    ])
    assert select_regression_tests(catalogue, manifest, "base") == []


def test_branch_selects_both_exemplar_workflows():
    catalogue = Catalogue(entries=[
        entry("normalise", tests=["normalise-contract"]),
        entry("product-workflow", ["normalise"], ["product-smoke"], CapabilityKind.flow),
        entry("report-workflow", ["normalise"], ["report-smoke"], CapabilityKind.flow),
    ])
    manifest = RunnerManifest(tests=[
        spec("normalise-contract", "normalise"),
        spec("product-smoke", "product-workflow", "smoke"),
        spec("report-smoke", "report-workflow", "smoke"),
    ])
    selected = select_regression_tests(catalogue, manifest, "normalise")
    assert [item.test.id for item in selected] == [
        "normalise-contract", "product-smoke", "report-smoke"
    ]


def test_cycle_terminates_and_does_not_reselect_changed_capability_as_consumer():
    catalogue = Catalogue(entries=[
        entry("a", ["c"], ["a-test"]),
        entry("b", ["a"], ["b-test"]),
        entry("c", ["b"], ["c-test"]),
    ])
    manifest = RunnerManifest(tests=[
        spec("a-test", "a"), spec("b-test", "b"), spec("c-test", "c"),
    ])
    selected = select_regression_tests(catalogue, manifest, "a")
    assert [item.test.id for item in selected] == ["a-test", "b-test", "c-test"]


def test_selection_explains_why_every_test_was_included():
    catalogue = Catalogue(entries=[entry("base"), entry("consumer", ["base"], ["smoke"])])
    selected = select_regression_tests(
        catalogue, RunnerManifest(tests=[spec("smoke", "consumer", "smoke")]), "base"
    )
    reason = selected[0].reasons[0]
    assert reason.via == "base"
    assert "dependency distance 1" in reason.explanation


def test_execution_runs_each_selected_test_and_returns_job_evidence():
    catalogue = Catalogue(entries=[
        entry("base", tests=["base-test"]), entry("consumer", ["base"], ["smoke"]),
    ])
    manifest = RunnerManifest(tests=[
        spec("base-test", "base"), spec("smoke", "consumer", "smoke")
    ])
    executor = Executor()
    result = run_regression_tests(catalogue, manifest, "base", executor=executor)
    assert result.passed is True
    assert executor.ran == ["base-test", "smoke"]
    assert [item.job_id for item in result.evidence] == ["job-base-test", "job-smoke"]
