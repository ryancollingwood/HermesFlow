"""Unit tests for f/hermes_flow/catalogue/models.py and windmill/capability-index.yaml
— not synced to Windmill (see conftest.py)."""
import json
import pathlib

import pytest
import yaml

from f.hermes_flow.catalogue.models import (
    CapabilityKind,
    Catalogue,
    CatalogueEntry,
    CatalogueValidationError,
    load_catalogue,
)
from f.libraries.capability.models import CapabilityMaturity

CATALOGUE_PATH = pathlib.Path(__file__).parent.parent / "capability-index.yaml"
WINDMILL_ROOT = pathlib.Path(__file__).parent.parent
SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"


def make_entry_yaml(path="f/foo/bar", **overrides) -> str:
    defaults = dict(
        kind="script",
        inputs_summary="x",
        outputs_summary="y",
        metadata=dict(
            path=path,
            capability_version="1.0.0",
            summary="a test capability",
            maturity="stable",
            owners=["x"],
        ),
    )
    defaults.update(overrides)
    return yaml.safe_dump({"entries": [defaults]})


# ── Empty catalogue ───────────────────────────────────────────────────────────


def test_empty_catalogue_is_valid():
    catalogue = load_catalogue("schema_version: '1.0'\nentries: []\n")
    assert catalogue.entries == []
    assert catalogue.list_scripts() == []
    assert catalogue.list_flows() == []


def test_catalogue_with_no_entries_key_defaults_to_empty():
    catalogue = load_catalogue("schema_version: '1.0'\n")
    assert catalogue.entries == []


# ── Valid catalogue ───────────────────────────────────────────────────────────


def test_valid_single_entry_catalogue():
    catalogue = load_catalogue(make_entry_yaml())
    assert len(catalogue.entries) == 1
    entry = catalogue.entries[0]
    assert entry.kind is CapabilityKind.script
    assert entry.metadata.path == "f/foo/bar"
    assert entry.metadata.maturity is CapabilityMaturity.stable


def test_get_finds_entry_by_path():
    catalogue = load_catalogue(make_entry_yaml(path="f/foo/bar"))
    assert catalogue.get("f/foo/bar") is not None
    assert catalogue.get("f/does/not/exist") is None


def test_list_scripts_and_flows_partition_by_kind():
    entries_yaml = yaml.safe_dump(
        {
            "entries": [
                {
                    "kind": "script",
                    "inputs_summary": "x",
                    "outputs_summary": "y",
                    "metadata": {
                        "path": "f/a/script",
                        "capability_version": "1.0.0",
                        "summary": "s",
                        "maturity": "stable",
                        "owners": ["x"],
                    },
                },
                {
                    "kind": "flow",
                    "inputs_summary": "x",
                    "outputs_summary": "y",
                    "metadata": {
                        "path": "f/a/flow",
                        "capability_version": "1.0.0",
                        "summary": "s",
                        "maturity": "stable",
                        "owners": ["x"],
                    },
                },
            ]
        }
    )
    catalogue = load_catalogue(entries_yaml)
    assert [e.metadata.path for e in catalogue.list_scripts()] == ["f/a/script"]
    assert [e.metadata.path for e in catalogue.list_flows()] == ["f/a/flow"]


# ── Duplicate paths ───────────────────────────────────────────────────────────


def test_duplicate_paths_rejected_with_path_named_in_error():
    entries_yaml = yaml.safe_dump(
        {
            "entries": [
                {
                    "kind": "script",
                    "inputs_summary": "x",
                    "outputs_summary": "y",
                    "metadata": {
                        "path": "f/dup/path",
                        "capability_version": "1.0.0",
                        "summary": "s1",
                        "maturity": "stable",
                        "owners": ["x"],
                    },
                },
                {
                    "kind": "script",
                    "inputs_summary": "x",
                    "outputs_summary": "y",
                    "metadata": {
                        "path": "f/dup/path",
                        "capability_version": "2.0.0",
                        "summary": "s2",
                        "maturity": "stable",
                        "owners": ["x"],
                    },
                },
            ]
        }
    )
    with pytest.raises(CatalogueValidationError, match="f/dup/path"):
        load_catalogue(entries_yaml)


# ── Malformed catalogues ──────────────────────────────────────────────────────


def test_malformed_yaml_rejected():
    with pytest.raises(CatalogueValidationError, match="not valid YAML"):
        load_catalogue("entries: [this is not: valid: yaml:")


def test_non_mapping_top_level_rejected():
    with pytest.raises(CatalogueValidationError, match="mapping"):
        load_catalogue("- just\n- a\n- list\n")


def test_entries_not_a_list_rejected():
    with pytest.raises(CatalogueValidationError, match="'entries' must be a list"):
        load_catalogue("entries: not-a-list\n")


def test_entry_missing_required_field_names_asset_and_field():
    # metadata.summary is missing
    bad = yaml.safe_dump(
        {
            "entries": [
                {
                    "kind": "script",
                    "inputs_summary": "x",
                    "outputs_summary": "y",
                    "metadata": {
                        "path": "f/broken/entry",
                        "capability_version": "1.0.0",
                        "maturity": "stable",
                        "owners": ["x"],
                    },
                }
            ]
        }
    )
    with pytest.raises(CatalogueValidationError) as exc_info:
        load_catalogue(bad)
    message = str(exc_info.value)
    assert "f/broken/entry" in message  # names the asset
    assert "metadata.summary" in message  # names the field


def test_entry_with_no_path_at_all_still_produces_a_locatable_error():
    # No metadata.path present — asset can't be named by path, falls back to index.
    bad = yaml.safe_dump(
        {
            "entries": [
                {
                    "kind": "script",
                    "inputs_summary": "x",
                    "outputs_summary": "y",
                    "metadata": {
                        "capability_version": "1.0.0",
                        "summary": "s",
                        "maturity": "stable",
                        "owners": ["x"],
                    },
                }
            ]
        }
    )
    with pytest.raises(CatalogueValidationError, match=r"entries\[0\]"):
        load_catalogue(bad)


def test_invalid_maturity_value_rejected():
    bad = make_entry_yaml(metadata=None)  # placeholder, overwritten below
    entry = yaml.safe_load(bad)
    entry["entries"][0]["metadata"] = {
        "path": "f/foo/bar",
        "capability_version": "1.0.0",
        "summary": "s",
        "maturity": "extremely-stable",  # not a real CapabilityMaturity value
        "owners": ["x"],
    }
    with pytest.raises(CatalogueValidationError, match="maturity"):
        load_catalogue(yaml.safe_dump(entry))


def test_entry_cannot_grant_automatic_promotion():
    # A catalogue entry inherits CapabilityMetadata's autonomy validator —
    # confirms the catalogue can't be used to smuggle an unsafe policy in.
    entry = yaml.safe_load(make_entry_yaml())
    entry["entries"][0]["metadata"]["autonomy"] = {"promote": "automatic"}
    with pytest.raises(CatalogueValidationError, match="promote"):
        load_catalogue(yaml.safe_dump(entry))


# ── Real capability-index.yaml: valid, and every path exists ────────────────


def test_real_capability_index_loads_and_validates():
    assert CATALOGUE_PATH.exists(), f"{CATALOGUE_PATH} is missing"
    catalogue = load_catalogue(CATALOGUE_PATH.read_text())
    assert isinstance(catalogue, Catalogue)
    assert len(catalogue.entries) >= 1


def test_real_capability_index_every_path_exists_under_windmill_f():
    catalogue = load_catalogue(CATALOGUE_PATH.read_text())
    missing = [
        entry.metadata.path
        for entry in catalogue.entries
        if not (
            (WINDMILL_ROOT / f"{entry.metadata.path}.py").exists()
            or (WINDMILL_ROOT / f"{entry.metadata.path}.flow" / "flow.yaml").exists()
        )
    ]
    assert not missing, (
        "catalogue entries reference paths with no matching .py or .flow/flow.yaml "
        f"asset: {missing}"
    )


def test_real_capability_index_no_duplicate_paths():
    catalogue = load_catalogue(CATALOGUE_PATH.read_text())
    paths = [e.metadata.path for e in catalogue.entries]
    assert len(paths) == len(set(paths))


# ── CatalogueEntry / Catalogue models used directly (not via YAML) ──────────


def test_catalogue_entry_can_be_constructed_directly():
    from f.libraries.capability.models import CapabilityEffects, CapabilityMetadata

    entry = CatalogueEntry(
        kind=CapabilityKind.script,
        tags=["example"],
        inputs_summary="none",
        outputs_summary="a string",
        metadata=CapabilityMetadata(
            path="f/example/thing",
            capability_version="1.0.0",
            summary="an example",
            maturity=CapabilityMaturity.experimental,
            owners=["x"],
            effects=CapabilityEffects(),
        ),
    )
    catalogue = Catalogue(entries=[entry])
    assert catalogue.get("f/example/thing") is entry


# ── docs/CI: checked-in JSON Schema export must match the model ─────────────


def test_checked_in_json_schema_matches_model():
    schema_path = SCHEMAS_DIR / "catalogue.schema.json"
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        "python -c \"import json; from f.hermes_flow.catalogue.models import Catalogue; "
        'print(json.dumps(Catalogue.model_json_schema(), indent=2, sort_keys=True))" '
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(Catalogue.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to Catalogue — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
