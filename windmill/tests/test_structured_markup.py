"""HF-022 deterministic structured-markup extraction tests."""
import json
from pathlib import Path

import pytest

from f.capabilities.collection.extract_structured_markup import (
    StructuredMarkupResult,
    extract_structured_markup,
)
from f.libraries.lineage.helpers import begin_lineage, write_artifact
from f.libraries.lineage.models import ArtifactStage
from f.libraries.storage.artifacts import FilesystemArtifactStore


FIXTURES = Path(__file__).parent / "fixtures" / "structured_markup"
SCHEMA_PATH = Path(__file__).parents[2] / "docs" / "schemas" / "structured_markup_result.schema.json"


def retained(tmp_path, content, media_type="text/html; charset=utf-8"):
    store = FilesystemArtifactStore(tmp_path)
    state, context = begin_lineage(
        capability="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        initiating_actor="test",
    )
    artifact = write_artifact(
        state,
        store,
        context,
        content,
        stage=ArtifactStage.raw,
        media_type=media_type,
        metadata={"kind": "raw_http_response", "url": "https://shop.example/product"},
    )
    return artifact, state, store


def fixture(tmp_path, name):
    return retained(tmp_path, (FIXTURES / name).read_bytes())


def test_valid_json_ld_returns_validated_candidate_and_provenance(tmp_path):
    artifact, state, store = fixture(tmp_path, "valid_product.html")
    result = extract_structured_markup(artifact, lineage=state, store=store)
    assert result.status == "success"
    assert result.blocks_found == result.blocks_parsed == 1
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.types == ["Product"]
    assert candidate.identifier == "urn:sku:SKU-100"
    assert candidate.name == "Trail Shoe"
    assert candidate.provenance.source_artifact_id == str(artifact.artifact_id)
    assert candidate.provenance.source_content_hash == artifact.content_hash
    assert candidate.provenance.source_url == "https://shop.example/product"
    assert candidate.provenance.source_path == "$"


def test_multiple_blocks_arrays_and_graphs_preserve_document_order(tmp_path):
    artifact, _, store = fixture(tmp_path, "multiple.html")
    result = extract_structured_markup(artifact, store=store)
    assert result.blocks_found == result.blocks_parsed == 2
    assert [candidate.name for candidate in result.candidates] == [
        "Example Shop", "Store", "Graph Product", None,
    ]
    assert [candidate.provenance.source_path for candidate in result.candidates] == [
        "$[0]", "$[1]", "$.@graph[0]", "$.@graph[1]",
    ]
    assert result.candidates[-1].types == ["Offer", "Thing"]


def test_malformed_block_does_not_hide_later_valid_block(tmp_path):
    artifact, _, store = fixture(tmp_path, "malformed.html")
    result = extract_structured_markup(artifact, store=store)
    assert result.status == "success"
    assert result.blocks_found == 2
    assert result.blocks_parsed == 1
    assert [candidate.types for candidate in result.candidates] == [["BreadcrumbList"]]
    assert result.warnings[0].code == "malformed_json_ld"
    assert result.warnings[0].block_index == 0


def test_absent_markup_is_a_successful_empty_result(tmp_path):
    artifact, _, store = fixture(tmp_path, "absent.html")
    result = extract_structured_markup(artifact, store=store)
    assert result.status == "no_markup"
    assert result.blocks_found == 0
    assert result.blocks_parsed == 0
    assert result.candidates == []
    assert result.warnings == []


def test_fixed_fixture_output_is_deterministic(tmp_path):
    artifact, state, store = fixture(tmp_path, "retail_graph.html")
    first = extract_structured_markup(artifact, lineage=state, store=store)
    second = extract_structured_markup(artifact, lineage=state, store=store)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [candidate.candidate_id for candidate in first.candidates] == [
        "fbe51a7b40f14ac69b9904cfb7f59fcace63e09341e2f05820a7624e661dea3a",
        "78b50165d7771bd222a61212263d399e01e415cc6883e38ed9f267d2aa42f614",
    ]


def test_direct_json_ld_artifact_is_supported(tmp_path):
    artifact, _, store = retained(
        tmp_path,
        b'{"@context":"https://schema.org","@type":"Organization","name":"Direct"}',
        media_type="application/ld+json",
    )
    result = extract_structured_markup(artifact, store=store)
    assert result.blocks_found == 1
    assert result.candidates[0].types == ["Organization"]


@pytest.mark.parametrize("payload", ["42", '"text"', "null", "[1,2]"])
def test_non_object_json_ld_shapes_are_reported_not_raised(tmp_path, payload):
    artifact, _, store = retained(
        tmp_path,
        f'<script type="application/ld+json">{payload}</script>',
    )
    result = extract_structured_markup(artifact, store=store)
    assert result.status == "no_valid_candidates"
    assert result.candidates == []
    assert result.warnings[0].code == "invalid_json_ld_shape"


def test_graph_ignores_non_object_items_with_warning(tmp_path):
    artifact, _, store = retained(
        tmp_path,
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"Product","name":"Good"},null,"bad"]}'
        "</script>",
    )
    result = extract_structured_markup(artifact, store=store)
    assert [candidate.name for candidate in result.candidates] == ["Good"]
    assert "ignored 2" in result.warnings[0].message


def test_context_only_and_empty_nodes_are_not_candidates(tmp_path):
    artifact, _, store = retained(
        tmp_path,
        '<script type="application/ld+json">[{},'
        '{"@context":"https://schema.org"},{"@type":"Product"}]</script>',
    )
    result = extract_structured_markup(artifact, store=store)
    assert len(result.candidates) == 1
    assert [warning.code for warning in result.warnings] == [
        "context_only_node", "context_only_node",
    ]


def test_block_candidate_and_json_size_limits_are_visible(tmp_path):
    html = "".join(
        f'<script type="application/ld+json">{{"@type":"Thing","name":"{index}"}}</script>'
        for index in range(3)
    )
    artifact, _, store = retained(tmp_path, html)
    block_limited = extract_structured_markup(artifact, store=store, max_blocks=2)
    assert block_limited.blocks_found == 3
    assert len(block_limited.candidates) == 2
    assert block_limited.warnings[0].code == "block_limit"
    candidate_limited = extract_structured_markup(artifact, store=store, max_candidates=1)
    assert len(candidate_limited.candidates) == 1
    assert candidate_limited.warnings[-1].code == "candidate_limit"
    json_limited = extract_structured_markup(artifact, store=store, max_json_bytes=10)
    assert json_limited.status == "no_valid_candidates"
    assert {warning.code for warning in json_limited.warnings} == {"block_too_large"}


def test_invalid_utf8_returns_structured_warning(tmp_path):
    artifact, _, store = retained(tmp_path, b"\xff\xfe")
    result = extract_structured_markup(artifact, store=store)
    assert result.status == "invalid_encoding"
    assert result.warnings[0].code == "invalid_encoding"


def test_lineage_mismatch_is_rejected(tmp_path):
    artifact, _, store = fixture(tmp_path, "valid_product.html")
    other_state, _ = begin_lineage(
        capability="other", capability_version="1", initiating_actor="test"
    )
    with pytest.raises(ValueError, match="not registered"):
        extract_structured_markup(artifact, lineage=other_state, store=store)


def test_contract_schema_matches_checked_in_copy():
    on_disk = json.loads(SCHEMA_PATH.read_text())
    current = json.loads(json.dumps(StructuredMarkupResult.model_json_schema(), sort_keys=True))
    assert on_disk == current
