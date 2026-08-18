"""HF-024 deterministic product normalisation."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from f.capabilities.collection.extract_products import (
    ProductExtractionResult,
    ProductProvenance,
)
from f.libraries.lineage.models import ArtifactRef
from pydantic import BaseModel, ConfigDict, Field

CAPABILITY_PATH = "f/capabilities/collection/normalise_products"
CAPABILITY_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"


class ValueStatus(str, Enum):
    valid = "valid"
    missing = "missing"
    invalid = "invalid"


class CurrencyStatus(str, Enum):
    valid = "valid"
    missing = "missing"
    ambiguous = "ambiguous"
    invalid = "invalid"
    conflict = "conflict"


class AvailabilityStatus(str, Enum):
    recognized = "recognized"
    missing = "missing"
    unrecognized = "unrecognized"


class Availability(str, Enum):
    in_stock = "in_stock"
    out_of_stock = "out_of_stock"
    preorder = "preorder"
    backorder = "backorder"
    discontinued = "discontinued"
    unknown = "unknown"


class OriginalOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Any = None
    currency: Any = None
    availability: Any = None
    seller: Any = None
    url: Any = None


class NormalizedOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
    price_status: ValueStatus
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    currency_status: CurrencyStatus
    availability: Availability
    availability_status: AvailabilityStatus
    seller: str | None = None
    url: str | None = None
    source_offer_index: int = Field(..., ge=0)
    original: OriginalOffer
    warnings: list[str] = Field(default_factory=list)


class NormalizedIdentifiers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str | None = None
    gtin: str | None = Field(default=None, pattern=r"^[0-9]{8,14}$")
    mpn: str | None = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)


class NormalizedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    name: str = Field(..., min_length=1)
    brand: str | None = None
    description: str | None = None
    canonical_url: str | None = None
    images: list[str] = Field(default_factory=list)
    identifiers: NormalizedIdentifiers
    offers: list[NormalizedOffer] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    provenance: ProductProvenance
    original: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class ProductNormalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    normalization_version: str = CAPABILITY_VERSION
    status: str
    source_schema_version: str
    source_artifact: ArtifactRef
    products: list[NormalizedProduct]
    warnings: list[str] = Field(default_factory=list)


_SPACE_RE = re.compile(r"\s+")
_CURRENCY_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3})(?![A-Za-z])")
_CURRENCY_ALIASES = {
    "AU$": "AUD", "A$": "AUD", "AUD$": "AUD",
    "US$": "USD", "USD$": "USD",
    "NZ$": "NZD", "NZD$": "NZD",
    "CA$": "CAD", "C$": "CAD", "CAD$": "CAD",
    "€": "EUR", "£": "GBP",
}
_AMBIGUOUS_SYMBOLS = {"$", "¥"}
_ISO_4217_CODES = frozenset(["AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BOV", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHE", "CHF", "CHW", "CLF", "CLP", "CNY", "COP", "COU", "CRC", "CUC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MXV", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SLL", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "USN", "UYI", "UYU", "UYW", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF", "XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XCD", "XCG", "XDR", "XOF", "XPD", "XPF", "XPT", "XSU", "XTS", "XUA", "XXX", "YER", "ZAR", "ZMW", "ZWG"])
_AVAILABILITY = {
    "instock": Availability.in_stock,
    "limitedavailability": Availability.in_stock,
    "outofstock": Availability.out_of_stock,
    "soldout": Availability.out_of_stock,
    "preorder": Availability.preorder,
    "presale": Availability.preorder,
    "backorder": Availability.backorder,
    "discontinued": Availability.discontinued,
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value))
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _identifier(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text else None


def _gtin(value: Any) -> tuple[str | None, bool]:
    text = _text(value)
    if not text:
        return None, False
    digits = re.sub(r"[\s-]", "", text)
    if digits.isdigit() and len(digits) in {8, 12, 13, 14}:
        return digits, False
    return None, True


def _currency_token(value: Any) -> tuple[str | None, CurrencyStatus]:
    text = _text(value)
    if not text:
        return None, CurrencyStatus.missing
    upper = text.upper().replace(" ", "")
    if upper in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[upper], CurrencyStatus.valid
    if text in _AMBIGUOUS_SYMBOLS:
        return None, CurrencyStatus.ambiguous
    if re.fullmatch(r"[A-Za-z]{3}", text):
        code = text.upper()
        return (code, CurrencyStatus.valid) if code in _ISO_4217_CODES else (
            None, CurrencyStatus.invalid
        )
    return None, CurrencyStatus.invalid


def _embedded_currency(text: str) -> tuple[str | None, CurrencyStatus, str]:
    working = text
    for token in sorted(_CURRENCY_ALIASES, key=len, reverse=True):
        if token.upper() in working.upper():
            code = _CURRENCY_ALIASES[token]
            working = re.sub(re.escape(token), "", working, flags=re.IGNORECASE)
            return code, CurrencyStatus.valid, working
    match = _CURRENCY_CODE_RE.search(working)
    if match:
        code = match.group(1).upper()
        working = working[:match.start()] + working[match.end():]
        if code in _ISO_4217_CODES:
            return code, CurrencyStatus.valid, working
        return None, CurrencyStatus.invalid, working
    for symbol in _AMBIGUOUS_SYMBOLS:
        if symbol in working:
            return None, CurrencyStatus.ambiguous, working.replace(symbol, "")
    return None, CurrencyStatus.missing, working


def _decimal_string(value: Any) -> tuple[str | None, ValueStatus, str | None, CurrencyStatus]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, ValueStatus.missing, None, CurrencyStatus.missing
    if isinstance(value, bool):
        return None, ValueStatus.invalid, None, CurrencyStatus.missing
    text = _text(value)
    if not text:
        return None, ValueStatus.missing, None, CurrencyStatus.missing
    embedded_code, embedded_status, numeric = _embedded_currency(text)
    numeric = numeric.replace("\u00a0", " ").strip()
    negative = numeric.startswith("(") and numeric.endswith(")")
    if negative:
        numeric = numeric[1:-1]
    numeric = re.sub(r"[\s'_]", "", numeric)
    if not re.fullmatch(r"[+-]?[0-9][0-9.,]*", numeric):
        return None, ValueStatus.invalid, embedded_code, embedded_status
    sign = ""
    if numeric[:1] in "+-":
        sign, numeric = numeric[0], numeric[1:]
    if negative:
        sign = "-"
    comma = numeric.rfind(",")
    dot = numeric.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_sep = "," if comma > dot else "."
    elif comma >= 0:
        tail = len(numeric) - comma - 1
        decimal_sep = "," if tail in {1, 2} else None
    elif dot >= 0:
        tail = len(numeric) - dot - 1
        decimal_sep = "." if tail in {1, 2} else None
    else:
        decimal_sep = None
    if decimal_sep:
        head, tail = numeric.rsplit(decimal_sep, 1)
        canonical = re.sub(r"[.,]", "", head) + "." + tail
    else:
        canonical = re.sub(r"[.,]", "", numeric)
    try:
        amount = Decimal(sign + canonical)
    except InvalidOperation:
        return None, ValueStatus.invalid, embedded_code, embedded_status
    if not amount.is_finite() or amount < 0:
        return None, ValueStatus.invalid, embedded_code, embedded_status
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0", ValueStatus.valid, embedded_code, embedded_status


def _normalise_currency(
    raw_currency: Any,
    embedded_code: str | None,
    embedded_status: CurrencyStatus,
) -> tuple[str | None, CurrencyStatus]:
    explicit_code, explicit_status = _currency_token(raw_currency)
    if explicit_status == CurrencyStatus.missing:
        return embedded_code, embedded_status
    if explicit_status != CurrencyStatus.valid:
        return explicit_code, explicit_status
    if embedded_code and embedded_code != explicit_code:
        return explicit_code, CurrencyStatus.conflict
    return explicit_code, CurrencyStatus.valid


def _normalise_availability(value: Any) -> tuple[Availability, AvailabilityStatus]:
    text = _text(value)
    if not text:
        return Availability.unknown, AvailabilityStatus.missing
    token = re.sub(r"[^a-z]", "", text.rsplit("/", 1)[-1].lower())
    normalized = _AVAILABILITY.get(token)
    if normalized is None:
        return Availability.unknown, AvailabilityStatus.unrecognized
    return normalized, AvailabilityStatus.recognized


def _normalise_offer(raw: dict[str, Any], index: int) -> NormalizedOffer:
    original = OriginalOffer.model_validate({
        key: raw.get(key) for key in OriginalOffer.model_fields
    })
    amount, price_status, embedded_code, embedded_status = _decimal_string(raw.get("price"))
    currency, currency_status = _normalise_currency(
        raw.get("currency"), embedded_code, embedded_status
    )
    availability, availability_status = _normalise_availability(raw.get("availability"))
    warnings = []
    if price_status == ValueStatus.invalid:
        warnings.append("price could not be parsed")
    if currency_status == CurrencyStatus.ambiguous:
        warnings.append("currency symbol is ambiguous")
    elif currency_status == CurrencyStatus.invalid:
        warnings.append("currency value is invalid")
    elif currency_status == CurrencyStatus.conflict:
        warnings.append("explicit and price-embedded currencies conflict")
    if availability_status == AvailabilityStatus.unrecognized:
        warnings.append("availability value is unrecognized")
    return NormalizedOffer(
        amount=amount,
        price_status=price_status,
        currency=currency,
        currency_status=currency_status,
        availability=availability,
        availability_status=availability_status,
        seller=_text(raw.get("seller")),
        url=_text(raw.get("url")),
        source_offer_index=index,
        original=original,
        warnings=warnings,
    )


def _normalise_product(raw: dict[str, Any]) -> NormalizedProduct:
    source_product_id = raw["product_id"]
    name = _text(raw.get("name"))
    if not name:
        raise ValueError("source product name is missing")
    sku = _identifier(raw.get("sku"))
    gtin, invalid_gtin = _gtin(raw.get("gtin"))
    mpn = _identifier(raw.get("mpn"))
    missing = [key for key, value in (("sku", sku), ("gtin", gtin), ("mpn", mpn)) if value is None]
    invalid = ["gtin"] if invalid_gtin else []
    if invalid_gtin and "gtin" in missing:
        missing.remove("gtin")
    identity = json.dumps(
        {"source_product_id": source_product_id, "schema_version": SCHEMA_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )
    warnings = list(raw.get("warnings") or [])
    if invalid_gtin:
        warnings.append("GTIN is invalid and was not normalized")
    return NormalizedProduct(
        normalized_product_id=hashlib.sha256(identity.encode()).hexdigest(),
        source_product_id=source_product_id,
        name=name,
        brand=_text(raw.get("brand")),
        description=_text(raw.get("description")),
        canonical_url=_text(raw.get("canonical_url")),
        images=list(dict.fromkeys(filter(None, (_text(item) for item in raw.get("images", []))))),
        identifiers=NormalizedIdentifiers(
            sku=sku, gtin=gtin, mpn=mpn, missing=missing, invalid=invalid
        ),
        offers=[
            _normalise_offer(offer, index)
            for index, offer in enumerate(raw.get("offers", []))
        ],
        attributes=dict(raw.get("attributes") or {}),
        provenance=ProductProvenance.model_validate(raw["provenance"]),
        original=raw,
        warnings=warnings,
    )


def normalise_products(
    product_extraction: ProductExtractionResult | ProductNormalizationResult | dict[str, Any],
) -> ProductNormalizationResult:
    """Normalize HF-023 output, or validate and return HF-024 output unchanged."""
    if isinstance(product_extraction, ProductNormalizationResult):
        return product_extraction
    raw = (
        product_extraction.model_dump(mode="json")
        if hasattr(product_extraction, "model_dump")
        else product_extraction
    )
    if not isinstance(raw, dict):
        raise TypeError("product_extraction must be an object")
    if "normalization_version" in raw:
        return ProductNormalizationResult.model_validate(raw)
    extraction = ProductExtractionResult.model_validate(raw)
    products = [
        _normalise_product(product.model_dump(mode="json"))
        for product in extraction.products
    ]
    return ProductNormalizationResult(
        status="success" if products else "no_products",
        source_schema_version=extraction.schema_version,
        source_artifact=extraction.source_artifact,
        products=products,
        warnings=[f"{warning.code}: {warning.message}" for warning in extraction.warnings],
    )


def main(product_extraction: dict) -> dict:
    return normalise_products(product_extraction).model_dump(mode="json")
