"""HF-024 deterministic product normalisation tests."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from f.capabilities.collection.extract_products import extract_products
from f.capabilities.collection.normalise_products import (
    ProductNormalizationResult,
    normalise_products,
)
from f.libraries.lineage.helpers import begin_lineage, write_artifact
from f.libraries.lineage.models import ArtifactStage
from f.libraries.storage.artifacts import FilesystemArtifactStore


FIXTURES = Path(__file__).parent / "fixtures" / "product_extraction"
SCHEMA_PATH = Path(__file__).parents[2] / "docs" / "schemas" / "product_normalization_result.schema.json"


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


def extracted_offer(tmp_path, price, currency, availability="InStock"):
    offer = {"price": price, "priceCurrency": currency, "availability": availability}
    html = (
        '<script type="application/ld+json">'
        + json.dumps({
            "@type": "Product",
            "name": "  Café   Mug  ",
            "brand": "  Example   Goods ",
            "sku": " mug-01 ",
            "gtin": "1234-5678-9012",
            "offers": offer,
        })
        + "</script>"
    ).encode()
    artifact, state, store = retained(tmp_path, html)
    return extract_products(artifact, lineage=state, store=store)


@pytest.mark.parametrize(
    ("price", "currency", "amount", "price_status", "code", "currency_status"),
    [
        ("1,234.50", "USD", "1234.5", "valid", "USD", "valid"),
        ("1.234,50 €", None, "1234.5", "valid", "EUR", "valid"),
        ("AUD 29.95", None, "29.95", "valid", "AUD", "valid"),
        ("1 234,50", "EUR", "1234.5", "valid", "EUR", "valid"),
        ("$12.00", None, "12", "valid", None, "ambiguous"),
        (None, None, None, "missing", None, "missing"),
        ("not a price", "AUD", None, "invalid", "AUD", "valid"),
        ("-1.00", "AUD", None, "invalid", "AUD", "valid"),
    ],
)
def test_price_and_currency_table(
    tmp_path, price, currency, amount, price_status, code, currency_status
):
    result = normalise_products(extracted_offer(tmp_path, price, currency))
    offer = result.products[0].offers[0]
    assert offer.amount == amount
    assert offer.price_status.value == price_status
    assert offer.currency == code
    assert offer.currency_status.value == currency_status
    assert offer.original.price == price
    assert offer.original.currency == currency


@pytest.mark.parametrize(
    ("raw", "normalized", "status"),
    [
        ("https://schema.org/InStock", "in_stock", "recognized"),
        ("sold out", "out_of_stock", "recognized"),
        ("PreOrder", "preorder", "recognized"),
        (None, "unknown", "missing"),
        ("ships eventually", "unknown", "unrecognized"),
    ],
)
def test_availability_table(tmp_path, raw, normalized, status):
    result = normalise_products(extracted_offer(tmp_path, "10", "AUD", raw))
    offer = result.products[0].offers[0]
    assert offer.availability.value == normalized
    assert offer.availability_status.value == status


def test_explicit_and_embedded_currency_conflict_is_visible(tmp_path):
    offer = normalise_products(
        extracted_offer(tmp_path, "US$ 12.00", "AUD")
    ).products[0].offers[0]
    assert offer.currency == "AUD"
    assert offer.currency_status.value == "conflict"
    assert offer.warnings == ["explicit and price-embedded currencies conflict"]


def test_text_identifiers_and_original_record_are_retained(tmp_path):
    extraction = extracted_offer(tmp_path, "29.95", "aud")
    source = extraction.products[0].model_dump(mode="json")
    product = normalise_products(extraction).products[0]
    assert product.name == "Café Mug"
    assert product.brand == "Example Goods"
    assert product.identifiers.sku == "MUG-01"
    assert product.identifiers.gtin == "123456789012"
    assert product.identifiers.missing == ["mpn"]
    assert product.original == source
    assert product.provenance == extraction.products[0].provenance


def test_invalid_gtin_is_explicit_and_original_is_preserved(tmp_path):
    extraction = extracted_offer(tmp_path, "10", "AUD")
    raw = extraction.model_dump(mode="json")
    raw["products"][0]["gtin"] = "ABC-123"
    product = normalise_products(raw).products[0]
    assert product.identifiers.gtin is None
    assert product.identifiers.invalid == ["gtin"]
    assert "gtin" not in product.identifiers.missing
    assert product.original["gtin"] == "ABC-123"


def test_normalisation_is_deterministic_and_idempotent(tmp_path):
    extraction = extracted_offer(tmp_path, "AUD 29.950", None)
    first = normalise_products(extraction)
    second = normalise_products(extraction.model_dump(mode="json"))
    third = normalise_products(first.model_dump(mode="json"))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.model_dump(mode="json") == third.model_dump(mode="json")


@pytest.mark.parametrize(
    ("fixture_name", "expected_amount", "currency_status", "availability"),
    [
        ("shopify.html", "29.95", "missing", "in_stock"),
        ("nextjs.html", "64", "valid", "in_stock"),
        ("opengraph.html", "35.5", "valid", "in_stock"),
    ],
)
def test_representative_source_regressions(
    tmp_path, fixture_name, expected_amount, currency_status, availability
):
    artifact, _, store = retained(tmp_path, (FIXTURES / fixture_name).read_bytes())
    extraction = extract_products(artifact, store=store)
    result = normalise_products(extraction)
    offer = result.products[0].offers[0]
    assert result.status == "success"
    assert offer.amount == expected_amount
    assert offer.currency_status.value == currency_status
    assert offer.availability.value == availability


def test_empty_extraction_is_an_explicit_empty_result(tmp_path):
    artifact, _, store = retained(tmp_path, (FIXTURES / "empty.html").read_bytes())
    extraction = extract_products(artifact, store=store)
    result = normalise_products(extraction)
    assert result.status == "no_products"
    assert result.products == []
    assert result.warnings == [
        "ai_fallback_not_configured: deterministic extraction found no product and Hermes fallback is disabled"
    ]


def test_contract_schema_matches_checked_in_copy():
    expected = ProductNormalizationResult.model_json_schema()
    Draft202012Validator.check_schema(expected)
    assert json.loads(SCHEMA_PATH.read_text()) == json.loads(
        json.dumps(expected, sort_keys=True)
    )
