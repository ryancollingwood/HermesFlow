"""HF-023 deterministic-first product extraction tests."""
import json
from pathlib import Path

from f.capabilities.collection.extract_products import (
    AIProductPayload,
    ProductExtractionResult,
    extract_products,
)
from f.libraries.lineage.helpers import begin_lineage, write_artifact
from f.libraries.lineage.models import ArtifactStage
from f.libraries.storage.artifacts import FilesystemArtifactStore
from jsonschema import Draft202012Validator

FIXTURES = Path(__file__).parent / "fixtures" / "product_extraction"
SCHEMA_PATH = Path(__file__).parents[2] / "docs" / "schemas" / "product_extraction_result.schema.json"


def retained(tmp_path, content, url="https://shop.example/products/item"):
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
        media_type="text/html; charset=utf-8",
        metadata={"kind": "raw_http_response", "url": url},
    )
    return artifact, state, store


def fixture(tmp_path, name, **kwargs):
    return retained(tmp_path, (FIXTURES / name).read_bytes(), **kwargs)


def test_structured_product_wins_before_known_parser_or_ai(tmp_path):
    html = b"""<script type="application/ld+json">
    {"@type":"Product","name":"Structured Shoe","sku":"S-1","offers":{"price":"20","priceCurrency":"AUD"}}
    </script><meta property="og:title" content="Wrong"><meta property="product:price:amount" content="99">"""
    artifact, state, store = retained(tmp_path, html)

    def should_not_run(*args, **kwargs):
        raise AssertionError("AI fallback must not run")

    result = extract_products(
        artifact, lineage=state, store=store, ai_invoker=should_not_run
    )
    assert result.status == "success"
    assert result.method == "structured_markup"
    assert result.products[0].name == "Structured Shoe"
    assert result.products[0].provenance.extraction_method == "structured_markup"
    assert result.attempted_methods == ["structured_markup"]


def test_shopify_known_parser_extracts_source_form_fields(tmp_path):
    artifact, _, store = fixture(tmp_path, "shopify.html")
    result = extract_products(
        artifact, {"source_type": "shopify"}, store=store
    )
    product = result.products[0]
    assert result.method == "known_parser:shopify"
    assert product.name == "Canvas Tote"
    assert product.brand == "Example Goods"
    assert product.sku == "TOTE-01"
    assert product.offers[0].price == "29.95"
    assert product.offers[0].availability == "InStock"
    assert product.provenance.evidence_paths[0].endswith("shopify_product_json")


def test_nextjs_known_parser_extracts_product(tmp_path):
    artifact, _, store = fixture(tmp_path, "nextjs.html")
    result = extract_products(artifact, {"source_type": "nextjs"}, store=store)
    product = result.products[0]
    assert result.method == "known_parser:nextjs"
    assert product.name == "Desk Lamp"
    assert product.brand == "Bright Co"
    assert product.canonical_url == "https://shop.example/desk-lamp"
    assert product.offers[0].currency == "AUD"


def test_opengraph_known_parser_extracts_product(tmp_path):
    artifact, _, store = fixture(tmp_path, "opengraph.html")
    result = extract_products(artifact, store=store)
    product = result.products[0]
    assert result.method == "known_parser:opengraph"
    assert product.name == "Wool Beanie"
    assert product.sku == "BEANIE-7"
    assert product.canonical_url == "https://shop.example/wool-beanie"
    assert product.images == ["https://cdn.example/beanie.jpg"]


def ai_payload(name="AI Product"):
    return {
        "products": [{
            "name": name,
            "brand": None,
            "sku": "AI-1",
            "gtin": None,
            "mpn": None,
            "description": None,
            "canonical_url": "https://shop.example/ai-product",
            "images": [],
            "offers": [{
                "price": "12.50",
                "currency": "AUD",
                "availability": None,
                "seller": None,
                "url": None,
            }],
        }],
        "warnings": ["price inferred from visible text"],
    }


def test_ai_fallback_receives_strict_schema_and_retains_provenance(tmp_path):
    artifact, _, store = fixture(tmp_path, "empty.html")
    captured = {}

    def invoker(conn, prompt, conversation, payload, schema, **kwargs):
        Draft202012Validator.check_schema(schema)
        captured.update({"payload": payload, "schema": schema})
        return {
            "status": "success",
            "parsed_output": ai_payload(),
            "artifacts": [{"artifact_id": "00000000-0000-0000-0000-000000000123"}],
        }

    result = extract_products(artifact, store=store, ai_invoker=invoker)
    product = result.products[0]
    assert result.method == "ai_fallback"
    assert product.name == "AI Product"
    assert product.provenance.extraction_method == "ai_fallback"
    assert product.provenance.ai_artifact_ids == [
        "00000000-0000-0000-0000-000000000123"
    ]
    assert captured["schema"] == AIProductPayload.model_json_schema()
    assert "html" in captured["payload"]
    assert any(warning.code == "ai_partial" for warning in result.warnings)


def test_malformed_ai_output_is_visible_and_not_returned(tmp_path):
    artifact, _, store = fixture(tmp_path, "empty.html")

    def invoker(*args, **kwargs):
        return {
            "status": "success",
            "parsed_output": {"products": [{"sku": "missing-name"}], "warnings": []},
            "artifacts": [],
        }

    result = extract_products(artifact, store=store, ai_invoker=invoker)
    assert result.status == "no_product_data"
    assert result.products == []
    assert result.warnings[-1].code == "ai_output_invalid"


def test_ai_request_failure_becomes_visible_warning(tmp_path):
    artifact, _, store = fixture(tmp_path, "empty.html")

    def invoker(*args, **kwargs):
        raise TimeoutError("Hermes timed out")

    result = extract_products(artifact, store=store, ai_invoker=invoker)
    assert result.status == "no_product_data"
    assert result.warnings[-1].code == "ai_request_failed"
    assert "timed out" in result.warnings[-1].message


def test_no_product_data_without_ai_is_normal_empty_result(tmp_path):
    artifact, _, store = fixture(tmp_path, "empty.html")
    result = extract_products(artifact, store=store)
    assert result.status == "no_product_data"
    assert result.method is None
    assert result.products == []
    assert result.warnings[-1].code == "ai_fallback_not_configured"


def test_duplicate_structured_records_are_deduplicated_with_warning(tmp_path):
    html = b"""<script type="application/ld+json">[
    {"@type":"Product","name":"Same","sku":"DUP-1"},
    {"@type":"Product","name":"Same again","sku":"DUP-1"}
    ]</script>"""
    artifact, _, store = retained(tmp_path, html)
    result = extract_products(artifact, store=store)
    assert len(result.products) == 1
    assert any(warning.code == "duplicate_product" for warning in result.warnings)


def test_partial_product_warnings_are_attached_to_record(tmp_path):
    html = b'<script type="application/ld+json">{"@type":"Product","name":"Only a name"}</script>'
    artifact, _, store = retained(tmp_path, html)
    result = extract_products(artifact, store=store)
    assert result.status == "success"
    assert result.products[0].warnings == [
        "no stable product identifier was extracted",
        "no offer or price was extracted",
    ]


def test_malformed_structured_markup_falls_through_to_known_parser(tmp_path):
    html = b"""<script type="application/ld+json">{"name": }</script>
    <meta property="og:title" content="Fallback Product">
    <meta property="product:price:amount" content="8.00">"""
    artifact, _, store = retained(tmp_path, html)
    result = extract_products(artifact, store=store)
    assert result.method == "known_parser:opengraph"
    assert result.products[0].name == "Fallback Product"
    assert any(warning.code == "structured_malformed_json_ld" for warning in result.warnings)


def test_supplied_source_metadata_overrides_retained_url(tmp_path):
    artifact, _, store = fixture(tmp_path, "opengraph.html")
    result = extract_products(
        artifact,
        {"source_url": "https://override.example/item?secret=x", "source_type": "catalog"},
        store=store,
    )
    provenance = result.products[0].provenance
    assert provenance.source_url == "https://override.example/item"
    assert provenance.source_type == "catalog"


def test_product_ids_and_output_are_deterministic(tmp_path):
    artifact, state, store = fixture(tmp_path, "nextjs.html")
    first = extract_products(artifact, lineage=state, store=store)
    second = extract_products(artifact, lineage=state, store=store)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_contract_schema_matches_checked_in_copy():
    assert json.loads(SCHEMA_PATH.read_text()) == json.loads(json.dumps(
        ProductExtractionResult.model_json_schema(), sort_keys=True
    ))
