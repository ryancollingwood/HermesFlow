"""HF-023 product extraction: structured data, known parsers, then Hermes."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from f.capabilities.collection.extract_structured_markup import (
    extract_structured_markup,
)
from f.hermes.client import hermes_endpoint
from f.libraries.ai.invoke_hermes_structured import invoke_hermes_structured
from f.libraries.lineage.helpers import LineageState
from f.libraries.lineage.models import ArtifactRef
from f.libraries.storage.artifacts import FilesystemArtifactStore
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CAPABILITY_PATH = "f/capabilities/collection/extract_products"
CAPABILITY_VERSION = "1.0.0"


class ExtractedOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: str | None
    currency: str | None
    availability: str | None
    seller: str | None
    url: str | None


class ProductProvenance(BaseModel):
    extraction_method: str
    extractor_version: str = CAPABILITY_VERSION
    source_artifact_id: str
    source_content_hash: str
    source_url: str | None = None
    source_type: str | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    structured_candidate_ids: list[str] = Field(default_factory=list)
    ai_artifact_ids: list[str] = Field(default_factory=list)


class ProductRecord(BaseModel):
    product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    name: str = Field(..., min_length=1)
    brand: str | None = None
    sku: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    description: str | None = None
    canonical_url: str | None = None
    images: list[str] = Field(default_factory=list)
    offers: list[ExtractedOffer] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    provenance: ProductProvenance
    warnings: list[str] = Field(default_factory=list)


class ProductExtractionWarning(BaseModel):
    code: str
    message: str
    evidence_path: str | None = None


class ProductExtractionResult(BaseModel):
    schema_version: str = "1.0"
    status: str
    method: str | None = None
    source_artifact: ArtifactRef
    products: list[ProductRecord]
    warnings: list[ProductExtractionWarning] = Field(default_factory=list)
    attempted_methods: list[str] = Field(default_factory=list)


class AIProductCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    brand: str | None
    sku: str | None
    gtin: str | None
    mpn: str | None
    description: str | None
    canonical_url: str | None
    images: list[str]
    offers: list[ExtractedOffer]


class AIProductPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[AIProductCandidate]
    warnings: list[str]


class _ProductHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.scripts: list[tuple[dict[str, str], str]] = []
        self._script_attrs: dict[str, str] | None = None
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key and "content" in attributes and key not in self.meta:
                self.meta[key] = attributes["content"]
        elif tag.lower() == "script" and self._script_attrs is None:
            self._script_attrs = attributes
            self._script_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_attrs is not None:
            self.scripts.append((self._script_attrs, "".join(self._script_parts)))
            self._script_attrs = None
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_attrs is not None:
            self._script_parts.append(data)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _brand(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def _url(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    return _text(value)


def _canonical_url(value: Any) -> str | None:
    url = _url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return url
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", "", ""))


def _images(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        url = _url(item)
        if url and url not in result:
            result.append(url)
    return result


def _offers(value: Any) -> list[ExtractedOffer]:
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        seller = item.get("seller")
        result.append(ExtractedOffer(
            price=_text(item.get("price") or item.get("lowPrice")),
            currency=_text(item.get("priceCurrency") or item.get("currency")),
            availability=_text(item.get("availability")),
            seller=_brand(seller),
            url=_canonical_url(item.get("url")),
        ))
    return [offer for offer in result if any(offer.model_dump().values())]


def _source_metadata(
    store: FilesystemArtifactStore,
    artifact: ArtifactRef,
    supplied: dict[str, Any] | None,
) -> dict[str, str | None]:
    retained = store.read_metadata(artifact.artifact_id).get("metadata", {})
    metadata = supplied or {}
    return {
        "source_url": _canonical_url(metadata.get("source_url") or retained.get("url")),
        "source_type": _text(metadata.get("source_type")),
    }


def _provenance(
    artifact: ArtifactRef,
    source: dict[str, str | None],
    method: str,
    evidence_paths: list[str],
    *,
    candidate_ids: list[str] | None = None,
    ai_artifact_ids: list[str] | None = None,
) -> ProductProvenance:
    return ProductProvenance(
        extraction_method=method,
        source_artifact_id=str(artifact.artifact_id),
        source_content_hash=artifact.content_hash,
        source_url=source["source_url"],
        source_type=source["source_type"],
        evidence_paths=evidence_paths,
        structured_candidate_ids=candidate_ids or [],
        ai_artifact_ids=ai_artifact_ids or [],
    )


def _record(
    data: dict[str, Any],
    provenance: ProductProvenance,
    *,
    evidence_path: str,
) -> tuple[ProductRecord | None, ProductExtractionWarning | None]:
    name = _text(data.get("name") or data.get("title"))
    if not name:
        return None, ProductExtractionWarning(
            code="missing_name", message="product candidate has no usable name",
            evidence_path=evidence_path,
        )
    gtin = next(
        (_text(data.get(key)) for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14")
         if _text(data.get(key))),
        None,
    )
    canonical = _canonical_url(data.get("url") or data.get("canonical_url"))
    brand = _brand(data.get("brand") or data.get("vendor"))
    sku = _text(data.get("sku") or data.get("productID") or data.get("id"))
    warnings = []
    if not any((canonical, sku, gtin, _text(data.get("mpn")))):
        warnings.append("no stable product identifier was extracted")
    offers = _offers(data.get("offers"))
    if not offers:
        warnings.append("no offer or price was extracted")
    identity = canonical or sku or gtin or f"{brand or ''}|{name}"
    product_id = hashlib.sha256(identity.strip().lower().encode()).hexdigest()
    reserved = {
        "@context", "@type", "@id", "name", "title", "brand", "vendor", "sku",
        "productID", "id", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn",
        "description", "url", "canonical_url", "image", "images", "offers",
    }
    return ProductRecord(
        product_id=product_id,
        name=name,
        brand=brand,
        sku=sku,
        gtin=gtin,
        mpn=_text(data.get("mpn")),
        description=_text(data.get("description")),
        canonical_url=canonical,
        images=_images(data.get("image") or data.get("images")),
        offers=offers,
        attributes={key: value for key, value in data.items() if key not in reserved},
        provenance=provenance,
        warnings=warnings,
    ), None


def _deduplicate(
    products: list[ProductRecord], warnings: list[ProductExtractionWarning]
) -> list[ProductRecord]:
    seen: dict[str, int] = {}
    unique = []
    for product in products:
        strong_tokens = [
            f"url:{product.canonical_url.lower()}" if product.canonical_url else None,
            f"sku:{product.sku.lower()}" if product.sku else None,
            f"gtin:{product.gtin}" if product.gtin else None,
        ]
        tokens = [token for token in strong_tokens if token] or [
            f"name:{(product.brand or '').lower()}|{product.name.lower()}"
        ]
        duplicate_of = next((seen[token] for token in tokens if token in seen), None)
        if duplicate_of is not None:
            warnings.append(ProductExtractionWarning(
                code="duplicate_product",
                message=f"duplicate product ignored; matches output index {duplicate_of}",
                evidence_path=(product.provenance.evidence_paths or [None])[0],
            ))
            continue
        index = len(unique)
        unique.append(product)
        for token in tokens:
            seen[token] = index
    return unique


def _structured_products(
    artifact: ArtifactRef,
    source: dict[str, str | None],
    markup_result,
    warnings: list[ProductExtractionWarning],
) -> list[ProductRecord]:
    products = []
    for warning in markup_result.warnings:
        warnings.append(ProductExtractionWarning(
            code=f"structured_{warning.code}",
            message=warning.message,
            evidence_path=f"jsonld[{warning.block_index}]" if warning.block_index is not None else None,
        ))
    for candidate in markup_result.candidates:
        if not any(item.lower() in {"product", "productmodel"} for item in candidate.types):
            continue
        path = f"jsonld[{candidate.provenance.block_index}]{candidate.provenance.source_path}"
        provenance = _provenance(
            artifact, source, "structured_markup", [path],
            candidate_ids=[candidate.candidate_id],
        )
        product, warning = _record(candidate.data, provenance, evidence_path=path)
        if product:
            products.append(product)
        if warning:
            warnings.append(warning)
    return products


def _json_scripts(parser: _ProductHtmlParser, predicate: Callable[[dict[str, str]], bool]):
    for index, (attrs, content) in enumerate(parser.scripts):
        if predicate(attrs):
            yield index, content


def _shopify_products(
    parser: _ProductHtmlParser,
    artifact: ArtifactRef,
    source: dict[str, str | None],
    warnings: list[ProductExtractionWarning],
) -> list[ProductRecord]:
    products = []
    scripts = _json_scripts(parser, lambda attrs: (
        attrs.get("id", "").lower().startswith("productjson-")
        or "data-product-json" in attrs
    ))
    for index, content in scripts:
        path = f"script[{index}]:shopify_product_json"
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            warnings.append(ProductExtractionWarning(
                code="malformed_shopify_json", message=str(exc), evidence_path=path
            ))
            continue
        data = value.get("product", value) if isinstance(value, dict) else None
        if not isinstance(data, dict):
            continue
        variants = data.get("variants") if isinstance(data.get("variants"), list) else []
        offers = [{
            "price": variant.get("price"),
            "availability": "InStock" if variant.get("available") is True else (
                "OutOfStock" if variant.get("available") is False else None
            ),
            "sku": variant.get("sku"),
        } for variant in variants if isinstance(variant, dict)]
        mapped = {
            **data,
            "name": data.get("title") or data.get("name"),
            "brand": data.get("vendor"),
            "sku": data.get("sku") or next((_text(item.get("sku")) for item in variants if isinstance(item, dict) and _text(item.get("sku"))), None),
            "url": data.get("url") or source["source_url"],
            "images": data.get("images") or data.get("featured_image"),
            "offers": offers,
        }
        product, warning = _record(
            mapped, _provenance(artifact, source, "known_parser:shopify", [path]),
            evidence_path=path,
        )
        if product:
            products.append(product)
        if warning:
            warnings.append(warning)
    return products


def _nextjs_products(
    parser: _ProductHtmlParser,
    artifact: ArtifactRef,
    source: dict[str, str | None],
    warnings: list[ProductExtractionWarning],
) -> list[ProductRecord]:
    products = []
    for index, content in _json_scripts(
        parser, lambda attrs: attrs.get("id", "").lower() == "__next_data__"
    ):
        path = f"script[{index}]:__NEXT_DATA__.props.pageProps.product"
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            warnings.append(ProductExtractionWarning(
                code="malformed_next_data", message=str(exc), evidence_path=path
            ))
            continue
        data = value
        for key in ("props", "pageProps", "product"):
            data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, dict):
            continue
        product, warning = _record(
            data, _provenance(artifact, source, "known_parser:nextjs", [path]),
            evidence_path=path,
        )
        if product:
            products.append(product)
        if warning:
            warnings.append(warning)
    return products


def _opengraph_products(
    parser: _ProductHtmlParser,
    artifact: ArtifactRef,
    source: dict[str, str | None],
    warnings: list[ProductExtractionWarning],
) -> list[ProductRecord]:
    meta = parser.meta
    if not meta.get("og:title") or not any(key.startswith("product:") for key in meta):
        return []
    path = "meta:opengraph_product"
    data = {
        "name": meta.get("og:title"),
        "description": meta.get("og:description") or meta.get("description"),
        "url": meta.get("og:url") or source["source_url"],
        "image": meta.get("og:image"),
        "brand": meta.get("product:brand"),
        "sku": meta.get("product:retailer_item_id"),
        "offers": {
            "price": meta.get("product:price:amount"),
            "priceCurrency": meta.get("product:price:currency"),
            "availability": meta.get("product:availability"),
        },
    }
    product, warning = _record(
        data, _provenance(artifact, source, "known_parser:opengraph", [path]),
        evidence_path=path,
    )
    if warning:
        warnings.append(warning)
    return [product] if product else []


def _ai_products(
    artifact: ArtifactRef,
    source: dict[str, str | None],
    html: str,
    ai_conn: dict,
    store: FilesystemArtifactStore,
    invoker,
    warnings: list[ProductExtractionWarning],
    max_ai_input_bytes: int,
) -> list[ProductRecord]:
    encoded = html.encode()
    if len(encoded) > max_ai_input_bytes:
        html = encoded[:max_ai_input_bytes].decode("utf-8", errors="ignore")
        warnings.append(ProductExtractionWarning(
            code="ai_input_truncated",
            message=f"AI input truncated to {max_ai_input_bytes} bytes",
        ))
    invoke = invoker or invoke_hermes_structured
    try:
        response = invoke(
            ai_conn,
            "Extract product records from the supplied HTML. Return only evidence present in the page.",
            [],
            {
                "source_url": source["source_url"],
                "source_type": source["source_type"],
                "html": html,
            },
            AIProductPayload.model_json_schema(),
            store=store,
            max_retries=1,
        )
    except Exception as exc:
        warnings.append(ProductExtractionWarning(
            code="ai_request_failed", message=str(exc)
        ))
        return []
    payload = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
    if payload.get("status") != "success":
        warnings.append(ProductExtractionWarning(
            code="ai_extraction_failed",
            message="Hermes structured extraction did not return a valid result",
        ))
        return []
    try:
        parsed = AIProductPayload.model_validate(payload.get("parsed_output"))
    except ValidationError as exc:
        warnings.append(ProductExtractionWarning(
            code="ai_output_invalid", message=str(exc)
        ))
        return []
    for warning in parsed.warnings:
        warnings.append(ProductExtractionWarning(code="ai_partial", message=warning))
    artifact_ids = [
        str(item.get("artifact_id"))
        for item in payload.get("artifacts", [])
        if isinstance(item, dict) and item.get("artifact_id")
    ]
    products = []
    for index, candidate in enumerate(parsed.products):
        path = f"ai.products[{index}]"
        product, warning = _record(
            candidate.model_dump(mode="json"),
            _provenance(
                artifact, source, "ai_fallback", [path], ai_artifact_ids=artifact_ids
            ),
            evidence_path=path,
        )
        if product:
            products.append(product)
        if warning:
            warnings.append(warning)
    return products


def extract_products(
    raw_artifact: ArtifactRef,
    source_metadata: dict[str, Any] | None = None,
    *,
    lineage: LineageState | None = None,
    store: FilesystemArtifactStore | None = None,
    ai_conn: dict | None = None,
    ai_invoker=None,
    max_html_bytes: int = 10_000_000,
    max_products: int = 100,
    max_ai_input_bytes: int = 200_000,
) -> ProductExtractionResult:
    if max_products <= 0 or max_products > 1000:
        raise ValueError("max_products must be between 1 and 1000")
    if max_ai_input_bytes <= 0 or max_ai_input_bytes > 2_000_000:
        raise ValueError("max_ai_input_bytes must be between 1 and 2000000")
    if lineage is not None and lineage.artifacts.get(raw_artifact.artifact_id) != raw_artifact:
        raise ValueError("raw artifact is not registered in the supplied lineage state")
    artifact_store = store or FilesystemArtifactStore()
    raw = artifact_store.read(raw_artifact)
    if len(raw) > max_html_bytes:
        raise ValueError(f"source artifact exceeds {max_html_bytes} bytes")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source artifact is not valid UTF-8") from exc
    source = _source_metadata(artifact_store, raw_artifact, source_metadata)
    warnings: list[ProductExtractionWarning] = []
    attempted = ["structured_markup"]
    markup = extract_structured_markup(
        raw_artifact, lineage=lineage, store=artifact_store, max_html_bytes=max_html_bytes
    )
    products = _structured_products(raw_artifact, source, markup, warnings)
    method = "structured_markup" if products else None

    if not products:
        parser = _ProductHtmlParser()
        parser.feed(html)
        parser.close()
        preferred = (source["source_type"] or "").lower()
        known = [
            ("known_parser:shopify", _shopify_products),
            ("known_parser:nextjs", _nextjs_products),
            ("known_parser:opengraph", _opengraph_products),
        ]
        if preferred:
            known.sort(key=lambda item: 0 if preferred in item[0] else 1)
        for known_method, parser_fn in known:
            attempted.append(known_method)
            products = parser_fn(parser, raw_artifact, source, warnings)
            if products:
                method = known_method
                break

    if not products:
        attempted.append("ai_fallback")
        if ai_conn is not None or ai_invoker is not None:
            products = _ai_products(
                raw_artifact,
                source,
                html,
                ai_conn or {"base_url": "mock", "api_key": "mock"},
                artifact_store,
                ai_invoker,
                warnings,
                max_ai_input_bytes,
            )
            if products:
                method = "ai_fallback"
        else:
            warnings.append(ProductExtractionWarning(
                code="ai_fallback_not_configured",
                message="deterministic extraction found no product and Hermes fallback is disabled",
            ))

    deduplicated = _deduplicate(products, warnings)
    products = deduplicated[:max_products]
    if len(deduplicated) > max_products:
        warnings.append(ProductExtractionWarning(
            code="product_limit", message=f"product output limited to {max_products}"
        ))
    return ProductExtractionResult(
        status="success" if products else "no_product_data",
        method=method,
        source_artifact=raw_artifact,
        products=products,
        warnings=warnings,
        attempted_methods=attempted,
    )


def main(
    raw_artifact: dict,
    source_metadata: dict = {},
    lineage_json: str = "",
    hermes_conn: hermes_endpoint = {},
    enable_ai_fallback: bool = False,
    max_html_bytes: int = 10_000_000,
    max_products: int = 100,
    max_ai_input_bytes: int = 200_000,
) -> dict:
    result = extract_products(
        ArtifactRef.model_validate(raw_artifact),
        source_metadata=source_metadata,
        lineage=LineageState.from_json(lineage_json) if lineage_json else None,
        ai_conn=hermes_conn if enable_ai_fallback else None,
        max_html_bytes=max_html_bytes,
        max_products=max_products,
        max_ai_input_bytes=max_ai_input_bytes,
    )
    return result.model_dump(mode="json")
