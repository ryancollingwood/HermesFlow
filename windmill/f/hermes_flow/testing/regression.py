"""HF-016 dependency-aware regression selection and execution."""
from __future__ import annotations

from f.hermes_flow.candidate_ops.diff import _consumer_impact
from f.hermes_flow.candidate_ops.models import is_candidate_path
from f.hermes_flow.catalogue.models import Catalogue, load_catalogue
from f.hermes_flow.repair.promote_fixture import SourceDriftFixture
from f.hermes_flow.testing.runner import (
    TestEvidence,
    TestExecutor,
    TestManifest,
    TestSpec,
    TestType,
    load_test_manifests,
    run_tests,
)
from pydantic import BaseModel


class SelectionReason(BaseModel):
    capability_path: str
    relationship: str
    distance: int
    via: str | None = None
    explanation: str


class SelectedRegressionTest(BaseModel):
    test: TestSpec
    run_for_capability: str
    reasons: list[SelectionReason]


class RegressionRunResult(BaseModel):
    schema_version: str = "1.0"
    changed_capability: str
    passed: bool
    selection: list[SelectedRegressionTest]
    evidence: list[TestEvidence]


def select_regression_tests(
    catalogue: Catalogue,
    manifest: TestManifest,
    changed_capability: str,
    promoted_fixtures: list[SourceDriftFixture | dict] | None = None,
    candidate_path: str | None = None,
) -> list[SelectedRegressionTest]:
    changed = catalogue.get(changed_capability)
    if changed is None:
        raise ValueError(f"changed capability {changed_capability!r} is not in the catalogue")
    by_id = {test.id: test for test in manifest.tests}
    selected: dict[str, SelectedRegressionTest] = {}

    def include(test_id: str, path: str, reason: SelectionReason, consumer_only: bool) -> None:
        if test_id not in by_id:
            raise ValueError(f"required test id {test_id!r} for {path!r} is missing from manifests")
        test = by_id[test_id]
        if path not in test.capability_paths:
            raise ValueError(f"test {test_id!r} does not declare capability {path!r}")
        if consumer_only and test.type not in (TestType.contract, TestType.smoke):
            return
        if test_id not in selected:
            selected[test_id] = SelectedRegressionTest(
                test=test, run_for_capability=path, reasons=[reason]
            )
        else:
            selected[test_id].reasons.append(reason)

    for test_id in changed.metadata.test_requirements:
        include(
            test_id,
            changed_capability,
            SelectionReason(
                capability_path=changed_capability,
                relationship="changed",
                distance=0,
                explanation="declared test of the changed capability; always included",
            ),
            consumer_only=False,
        )

    for impact in _consumer_impact(catalogue, changed_capability):
        consumer = catalogue.get(impact["path"])
        if consumer is None:
            continue
        for test_id in consumer.metadata.test_requirements:
            include(
                test_id,
                consumer.metadata.path,
                SelectionReason(
                    capability_path=consumer.metadata.path,
                    relationship=impact["impact"],
                    distance=impact["distance"],
                    via=impact["via"],
                    explanation=(
                        f"{impact['impact']} consumer at dependency distance "
                        f"{impact['distance']}; contract/smoke regression required"
                    ),
                ),
                consumer_only=True,
            )

    fixtures = [
        item if isinstance(item, SourceDriftFixture) else SourceDriftFixture.model_validate(item)
        for item in (promoted_fixtures or [])
    ]
    matching = [item for item in fixtures if item.capability_path == changed_capability]
    if matching and not candidate_path:
        raise ValueError("candidate_path is required when selecting promoted source-drift fixtures")
    if matching and not is_candidate_path(candidate_path):
        raise ValueError("promoted source-drift fixtures may target only isolated candidates")
    for fixture in matching:
        test = TestSpec(
            id=fixture.fixture_id,
            capability_paths=[changed_capability],
            type=TestType.fixture,
            mode="promotion_gating",
            script_path="f/hermes_flow/testing/source_drift_fixture",
            args={
                "fixture_record": fixture.model_dump(mode="json"),
                "candidate_path": candidate_path,
            },
            timeout_seconds=fixture.binding.timeout_seconds,
            max_data_bytes=fixture.binding.max_data_bytes,
        )
        if test.id in selected:
            raise ValueError(f"promoted fixture id {test.id!r} conflicts with a manifest test")
        selected[test.id] = SelectedRegressionTest(
            test=test,
            run_for_capability=changed_capability,
            reasons=[SelectionReason(
                capability_path=changed_capability,
                relationship="promoted_source_drift",
                distance=0,
                explanation=(
                    f"sanitised source artifact from failed job {fixture.failed_job_id}; "
                    "candidate fixture regression required"
                ),
            )],
        )
    return [selected[test_id] for test_id in sorted(selected)]


def run_regression_tests(
    catalogue: Catalogue,
    manifest: TestManifest,
    changed_capability: str,
    max_timeout_seconds: int = 300,
    max_data_bytes: int = 5_000_000,
    executor: TestExecutor | None = None,
    promoted_fixtures: list[SourceDriftFixture | dict] | None = None,
    candidate_path: str | None = None,
) -> RegressionRunResult:
    selection = select_regression_tests(
        catalogue,
        manifest,
        changed_capability,
        promoted_fixtures=promoted_fixtures,
        candidate_path=candidate_path,
    )
    manifest_ids = {test.id for test in manifest.tests}
    execution_manifest = TestManifest(
        schema_version=manifest.schema_version,
        tests=[
            *manifest.tests,
            *(item.test for item in selection if item.test.id not in manifest_ids),
        ],
    )
    evidence: list[TestEvidence] = []
    for selected in selection:
        result = run_tests(
            execution_manifest,
            selected.run_for_capability,
            [selected.test.id],
            max_timeout_seconds=max_timeout_seconds,
            max_data_bytes=max_data_bytes,
            executor=executor,
        )
        evidence.extend(result.evidence)
    return RegressionRunResult(
        changed_capability=changed_capability,
        passed=bool(evidence) and all(item.status.value != "failed" for item in evidence),
        selection=selection,
        evidence=evidence,
    )


def main(
    catalogue_yaml: str,
    manifest_yaml: str,
    changed_capability: str,
    max_timeout_seconds: int = 300,
    max_data_bytes: int = 5_000_000,
    promoted_fixtures: list[dict] | None = None,
    candidate_path: str = "",
) -> dict:
    result = run_regression_tests(
        load_catalogue(catalogue_yaml),
        load_test_manifests(manifest_yaml),
        changed_capability,
        max_timeout_seconds=max_timeout_seconds,
        max_data_bytes=max_data_bytes,
        promoted_fixtures=promoted_fixtures,
        candidate_path=candidate_path or None,
    )
    return result.model_dump(mode="json")
