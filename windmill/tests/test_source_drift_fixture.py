"""HF-031 source-drift fixture promotion, sanitisation, selection, and execution tests."""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from f.hermes_flow.catalogue.models import CapabilityKind, Catalogue, CatalogueEntry
from f.hermes_flow.repair.promote_fixture import (
    FixturePromotionError,
    SourceDriftFixture,
    promote_source_drift_fixture,
)
from f.hermes_flow.testing.regression import (
    run_regression_tests,
    select_regression_tests,
)
from f.hermes_flow.testing.runner import TestManifest as RunnerManifest
from f.hermes_flow.testing.source_drift_fixture import (
    SourceDriftFixtureRunResult,
    run_source_drift_fixture,
)
from f.libraries.capability.models import CapabilityMaturity, CapabilityMetadata
from f.libraries.lineage.models import ArtifactStage
from f.libraries.storage.artifacts import FilesystemArtifactStore

CAPABILITY = "f/capabilities/collection/source_selector"
CANDIDATE = "f/hermes_flow/candidates/aaaaaaaaaaaaaaaa"
OLD_HTML = '''<!doctype html>
<!-- build 2026-07-14 -->
<html><head><meta name="csrf-token" content="csrf-secret"></head>
<body nonce="volatile-nonce" data-request-id="request-123">
<article class="old-card" data-token="source-secret">Old product</article>
</body></html>'''
NEW_HTML = '''<!doctype html>
<html><body data-timestamp="2026-07-14T00:00:00Z">
<article class="product-card" authorization="Bearer top-secret">New product</article>
</body></html>'''


def source_artifact(store, content, media_type):
    return store.write(
        content,
        trace_id=uuid4(),
        stage=ArtifactStage.raw,
        creator_capability="f/capabilities/collection/web_fetch",
        creator_capability_version="1.0.0",
        media_type=media_type,
        metadata={"kind": "failed_source"},
    )


def expected():
    return {
        "description": "The repaired selector finds exactly one product card.",
        "required_paths": ["matched_count"],
        "expected_values": {"matched_count": 1},
    }


def promote_html(store, content, failed_job_id="failed-source-drift"):
    source = source_artifact(store, content, "text/html; charset=utf-8")
    return promote_source_drift_fixture(
        source,
        failed_job_id,
        CAPABILITY,
        expected(),
        binding={"fixture_argument": "source_html", "payload_mode": "text"},
        store=store,
    ), source


def catalogue():
    return Catalogue(entries=[CatalogueEntry(
        kind=CapabilityKind.script,
        tags=["selector"],
        inputs_summary="HTML",
        outputs_summary="matches",
        metadata=CapabilityMetadata(
            path=CAPABILITY,
            capability_version="1.0.0",
            summary="Select product cards",
            maturity=CapabilityMaturity.stable,
            owners=["platform"],
        ),
    )])


class CandidateClient:
    """Represents the repaired selector accepting both old and drifted markup."""

    def __init__(self):
        self.calls = []

    def run_script_by_path_async(self, path, args):
        self.calls.append((path, args))
        return f"candidate-job-{len(self.calls)}"

    def wait_job(self, job_id, timeout, cleanup):
        html = self.calls[-1][1]["source_html"]
        return {
            "matched_count": html.count('class="old-card"')
            + html.count('class="product-card"')
        }

    def cancel_job(self, job_id, reason):
        raise AssertionError("candidate should not time out")


class FixtureExecutor:
    def __init__(self, store, candidate_client):
        self.store = store
        self.candidate_client = candidate_client
        self.ran = []

    def run(self, spec):
        self.ran.append(spec.id)
        result = run_source_drift_fixture(
            spec.args["fixture_record"],
            spec.args["candidate_path"],
            client=self.candidate_client,
            store=self.store,
        )
        return f"fixture-job-{len(self.ran)}", result.model_dump(mode="json")


def test_html_fixture_is_content_addressed_failure_linked_and_sanitised(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    fixture, source = promote_html(store, OLD_HTML)
    retained = store.read_text(fixture.fixture_artifact)
    assert fixture.fixture_artifact.content_hash in fixture.fixture_id
    assert fixture.failed_job_id == "failed-source-drift"
    assert fixture.source_artifact_id == source.artifact_id
    assert fixture.fixture_artifact.derived_from == [source.artifact_id]
    assert 'class="old-card"' in retained
    assert "csrf-secret" not in retained
    assert "source-secret" not in retained
    assert "volatile-nonce" not in retained
    assert "request-123" not in retained
    assert "build 2026" not in retained
    assert fixture.sanitization.redaction_count >= 2
    assert fixture.sanitization.removal_count >= 3
    metadata = store.read_metadata(fixture.fixture_artifact.artifact_id)["metadata"]
    assert metadata["failed_job_id"] == "failed-source-drift"
    assert metadata["expected_behavior"]["expected_values"] == {"matched_count": 1}


def test_json_fixture_redacts_sensitive_and_removes_volatile_fields_deterministically(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    payload = {
        "products": [{
            "name": "Widget",
            "api_token": "secret-token",
            "generated_at": "2026-07-14T00:00:00Z",
            "nested": {"password": "do-not-retain", "price": 10},
        }],
        "request_id": "volatile-request",
    }
    source = source_artifact(store, json.dumps(payload), "application/json")
    kwargs = dict(
        source_artifact=source,
        failed_job_id="failed-json-job",
        capability_path=CAPABILITY,
        expected_behavior={
            "description": "Product remains extractable.",
            "minimum_item_counts": {"products": 1},
            "expected_values": {"products.0.name": "Widget"},
        },
        binding={"fixture_argument": "payload", "payload_mode": "json"},
        store=store,
    )
    first = promote_source_drift_fixture(**kwargs)
    second = promote_source_drift_fixture(**kwargs)
    retained = json.loads(store.read(first.fixture_artifact))
    assert retained["products"][0]["api_token"] == "[SANITISED]"
    assert retained["products"][0]["nested"]["password"] == "[SANITISED]"
    assert "generated_at" not in retained["products"][0]
    assert "request_id" not in retained
    assert retained["products"][0]["nested"]["price"] == 10
    assert first.fixture_id == second.fixture_id
    assert first.fixture_artifact.content_hash == second.fixture_artifact.content_hash


def test_custom_fields_can_be_redacted_or_removed(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    source = source_artifact(
        store, json.dumps({"customer_email": "person@example.test", "build": "123"}),
        "application/json",
    )
    fixture = promote_source_drift_fixture(
        source,
        "failed-custom-job",
        CAPABILITY,
        {
            "description": "Custom fixture fields are sanitised.",
            "required_paths": ["customer_email"],
        },
        sanitization_rules={
            "redact_fields": ["customer_email"],
            "remove_fields": ["build"],
        },
        store=store,
    )
    retained = json.loads(store.read(fixture.fixture_artifact))
    assert retained == {"customer_email": "[SANITISED]"}


def test_embedded_json_script_is_recursively_sanitised(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    html = (
        '<html><script type="application/ld+json">'
        '{"name":"Widget","password":"hidden","request_id":"volatile"}'
        "</script></html>"
    )
    fixture, _ = promote_html(store, html)
    retained = store.read_text(fixture.fixture_artifact)
    assert '"name":"Widget"' in retained
    assert '"password":"[SANITISED]"' in retained
    assert "hidden" not in retained
    assert "request_id" not in retained
    assert "volatile" not in retained


@pytest.mark.parametrize(
    ("expected_behavior", "binding", "message"),
    [
        (
            {
                "description": "Do not retain secrets.",
                "expected_values": {"api_token": "secret-value"},
            },
            None,
            "expected behavior",
        ),
        (
            {"description": "No secret args.", "required_paths": ["status"]},
            {"candidate_args": {"password": "hardcoded"}},
            "candidate_args",
        ),
    ],
)
def test_sensitive_expected_metadata_or_candidate_args_are_rejected(
    tmp_path, expected_behavior, binding, message
):
    store = FilesystemArtifactStore(tmp_path)
    source = source_artifact(store, "<html></html>", "text/html")
    with pytest.raises(ValueError, match=message):
        promote_source_drift_fixture(
            source,
            "failed-secret-metadata",
            CAPABILITY,
            expected_behavior,
            binding=binding,
            store=store,
        )


def test_invalid_json_is_rejected_without_writing_fixture(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    source = source_artifact(store, "{not-json", "application/json")
    with pytest.raises(FixturePromotionError, match="not valid JSON"):
        promote_source_drift_fixture(
            source,
            "failed",
            CAPABILITY,
            {"description": "Must parse.", "required_paths": ["products"]},
            store=store,
        )
    assert list(tmp_path.glob("metadata/*.json")) == [
        tmp_path / "metadata" / f"{source.artifact_id}.json"
    ]


def test_invalid_format_or_failure_link_is_rejected_before_fixture_write(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    source = source_artifact(store, "<html></html>", "text/html")
    with pytest.raises(FixturePromotionError, match="fixture_format"):
        promote_source_drift_fixture(
            source,
            "failed",
            CAPABILITY,
            {"description": "Must parse.", "required_paths": ["status"]},
            fixture_format="xml",
            store=store,
        )
    with pytest.raises(FixturePromotionError, match="failed_job_id"):
        promote_source_drift_fixture(
            source,
            "",
            CAPABILITY,
            {"description": "Must parse.", "required_paths": ["status"]},
            store=store,
        )
    assert len(list(tmp_path.glob("metadata/*.json"))) == 1


def test_promoted_fixture_is_added_to_candidate_regression_selection(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    fixture, _ = promote_html(store, NEW_HTML)
    selected = select_regression_tests(
        catalogue(),
        RunnerManifest(tests=[]),
        CAPABILITY,
        promoted_fixtures=[fixture],
        candidate_path=CANDIDATE,
    )
    assert [item.test.id for item in selected] == [fixture.fixture_id]
    assert selected[0].test.type.value == "fixture"
    assert selected[0].test.script_path == "f/hermes_flow/testing/source_drift_fixture"
    assert selected[0].test.args["candidate_path"] == CANDIDATE
    assert selected[0].reasons[0].relationship == "promoted_source_drift"


def test_candidate_path_is_required_for_promoted_fixture_selection(tmp_path):
    fixture, _ = promote_html(FilesystemArtifactStore(tmp_path), NEW_HTML)
    with pytest.raises(ValueError, match="candidate_path is required"):
        select_regression_tests(
            catalogue(), RunnerManifest(tests=[]), CAPABILITY, promoted_fixtures=[fixture]
        )


def test_promoted_fixture_cannot_execute_an_active_path(tmp_path):
    fixture, _ = promote_html(FilesystemArtifactStore(tmp_path), NEW_HTML)
    with pytest.raises(ValueError, match="only isolated candidates"):
        select_regression_tests(
            catalogue(),
            RunnerManifest(tests=[]),
            CAPABILITY,
            promoted_fixtures=[fixture],
            candidate_path=CAPABILITY,
        )


def test_repaired_candidate_runs_against_old_and_new_promoted_fixtures(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    old_fixture, _ = promote_html(store, OLD_HTML, "old-baseline-job")
    new_fixture, _ = promote_html(store, NEW_HTML, "failed-drift-job")
    candidate_client = CandidateClient()
    executor = FixtureExecutor(store, candidate_client)
    result = run_regression_tests(
        catalogue(),
        RunnerManifest(tests=[]),
        CAPABILITY,
        promoted_fixtures=[old_fixture, new_fixture],
        candidate_path=CANDIDATE,
        executor=executor,
    )
    assert result.passed is True
    assert len(result.evidence) == 2
    assert all(item.status.value == "passed" for item in result.evidence)
    assert len(candidate_client.calls) == 2
    assert all(call[0] == CANDIDATE for call in candidate_client.calls)


def test_fixture_runner_reports_failed_expected_behavior(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    fixture, _ = promote_html(store, "<html><body>No products</body></html>")
    result = run_source_drift_fixture(
        fixture,
        CANDIDATE,
        client=CandidateClient(),
        store=store,
    )
    assert result.status == "fail"
    assert result.assertions[0].passed is True
    assert result.assertions[1].passed is False


SCHEMA_ROOT = Path(__file__).parents[2] / "docs/schemas"


def test_checked_in_fixture_schema_matches_model():
    assert json.loads((SCHEMA_ROOT / "source_drift_fixture.schema.json").read_text()) == (
        SourceDriftFixture.model_json_schema()
    )


def test_checked_in_fixture_run_schema_matches_model():
    assert json.loads((SCHEMA_ROOT / "source_drift_fixture_run.schema.json").read_text()) == (
        SourceDriftFixtureRunResult.model_json_schema()
    )
