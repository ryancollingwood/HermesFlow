"""HF-012 candidate diff and impact-analysis tests."""
from copy import deepcopy

from f.hermes_flow.candidate_ops.diff import analyse_candidate
from f.hermes_flow.catalogue.models import Catalogue
from f.libraries.capability.models import CapabilityMetadata, CapabilityMaturity
from f.hermes_flow.catalogue.models import CapabilityKind, CatalogueEntry


def entry(path, dependencies=(), tests=()):
    return CatalogueEntry(
        kind=CapabilityKind.script,
        tags=[],
        inputs_summary="input",
        outputs_summary="output",
        metadata=CapabilityMetadata(
            path=path,
            capability_version="1.0.0",
            summary=path,
            maturity=CapabilityMaturity.stable,
            owners=["platform"],
            dependencies=list(dependencies),
            test_requirements=list(tests),
        ),
    )


ACTIVE_PATH = "f/capabilities/base"
CANDIDATE_PATH = "f/hermes_flow/candidates/abc"


def analyse(active, candidate, catalogue=None, candidate_metadata=None):
    catalogue = catalogue or Catalogue(entries=[entry(ACTIVE_PATH, tests=["tests/base"])])
    return analyse_candidate(
        active=active,
        candidate=candidate,
        active_path=ACTIVE_PATH,
        candidate_path=CANDIDATE_PATH,
        catalogue=catalogue,
        candidate_capability_metadata=candidate_metadata,
    )


def snapshot(content="x = 1\n", schema=None, summary="base"):
    return {
        "content": content,
        "schema": schema or {"type": "object", "properties": {}},
        "summary": summary,
        "description": "",
        "language": "python3",
        "tag": None,
    }


def test_code_only_change_has_unified_diff():
    result = analyse(snapshot(), snapshot(content="x = 2\n"))
    assert result["change_categories"] == {
        "code": True, "schema": False, "metadata": False, "dependencies": False
    }
    assert "-x = 1" in result["diff"]["code"]["unified_diff"]
    assert "+x = 2" in result["diff"]["code"]["unified_diff"]


def test_schema_only_change_reports_leaf_path():
    candidate = snapshot(schema={"type": "object", "properties": {"limit": {"type": "integer"}}})
    result = analyse(snapshot(), candidate)
    assert result["change_categories"]["schema"] is True
    assert result["diff"]["schema"]["changes"][0]["path"] == "properties.limit"


def test_metadata_only_change():
    result = analyse(snapshot(), snapshot(summary="new summary"))
    assert result["change_categories"] == {
        "code": False, "schema": False, "metadata": True, "dependencies": False
    }


def test_combined_changes_and_dependency_delta():
    catalogue = Catalogue(entries=[entry(ACTIVE_PATH, dependencies=["f/lib/old"])])
    proposed = catalogue.get(ACTIVE_PATH).metadata.model_dump(mode="json")
    proposed["dependencies"] = ["f/lib/new"]
    proposed["summary"] = "changed metadata"
    result = analyse(
        snapshot(), snapshot(content="changed\n", schema={"type": "string"}),
        catalogue, proposed,
    )
    assert all(result["change_categories"].values())
    assert result["diff"]["dependencies"]["added"] == ["f/lib/new"]
    assert result["diff"]["dependencies"]["removed"] == ["f/lib/old"]


def test_no_change_candidate_is_explicitly_detected():
    result = analyse(snapshot(), deepcopy(snapshot()))
    assert result["no_changes"] is True
    assert result["promotion_summary"]["text"].startswith("No changes detected")


def test_direct_and_transitive_consumers_link_tests():
    catalogue = Catalogue(entries=[
        entry(ACTIVE_PATH, tests=["tests/base"]),
        entry("f/workflows/direct", [ACTIVE_PATH], ["tests/direct"]),
        entry("f/workflows/transitive", ["f/workflows/direct"], ["tests/transitive"]),
    ])
    result = analyse(snapshot(), snapshot(content="changed\n"), catalogue)
    consumers = result["impact"]["consumers"]
    assert [(x["path"], x["impact"]) for x in consumers] == [
        ("f/workflows/direct", "direct"),
        ("f/workflows/transitive", "transitive"),
    ]
    assert result["promotion_summary"]["required_tests"] == [
        "tests/base", "tests/direct", "tests/transitive"
    ]


def test_dependency_cycle_terminates_without_reporting_changed_capability_as_consumer():
    catalogue = Catalogue(entries=[
        entry(ACTIVE_PATH, ["f/workflows/b"]),
        entry("f/workflows/b", [ACTIVE_PATH], ["tests/b"]),
    ])
    result = analyse(snapshot(), snapshot(content="changed\n"), catalogue)
    assert [x["path"] for x in result["impact"]["consumers"]] == ["f/workflows/b"]
