"""HF-026 deterministic comparison of persisted normalized product snapshots."""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any, Optional, TypedDict
from uuid import UUID

import psycopg2
from pydantic import BaseModel, ConfigDict, Field

from f.capabilities.collection.normalise_products import (
    CurrencyStatus,
    NormalizedProduct,
    ValueStatus,
)

CAPABILITY_PATH = "f/capabilities/collection/compare_product_snapshots"
CAPABILITY_VERSION = "1.0.0"


class postgresql(TypedDict):
    host: str
    port: int
    user: str
    dbname: str
    password: str
    sslmode: str


class SnapshotSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_trace_id: UUID
    label: Optional[str] = None


class SnapshotRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int = Field(..., ge=1)
    execution_trace_id: UUID
    source_trace_id: UUID
    source_artifact_id: UUID
    source_content_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    normalized_product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_product_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    product_payload: NormalizedProduct


class ComparisonWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    execution_trace_id: Optional[UUID] = None
    source_artifact_id: Optional[UUID] = None
    normalized_product_id: Optional[str] = None


class SourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_trace_id: UUID
    source_trace_id: Optional[UUID] = None
    source_artifact_id: Optional[UUID] = None
    source_content_hash: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    label: Optional[str] = None
    product_count: int = Field(..., ge=0)
    unique_product_count: int = Field(..., ge=0)
    priced_product_count: int = Field(..., ge=0)
    duplicate_product_count: int = Field(..., ge=0)


class ProductObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    execution_trace_id: UUID
    source_artifact_id: UUID
    normalized_product_id: str
    source_product_id: str
    name: str
    brand: Optional[str] = None
    sku: Optional[str] = None
    gtin: Optional[str] = None
    mpn: Optional[str] = None
    canonical_url: Optional[str] = None
    prices: dict[str, str] = Field(default_factory=dict)


class SourcePrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_artifact_id: UUID
    normalized_product_id: str
    amount: str


class PriceDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    values: list[SourcePrice]
    minimum: str
    maximum: str
    absolute_difference: str
    percentage_difference: Optional[str] = None


class ProductComparisonGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_key: str
    display_name: str
    observations: list[ProductObservation]
    price_differences: list[PriceDifference] = Field(default_factory=list)


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_source_count: int = Field(..., ge=0)
    covered_source_count: int = Field(..., ge=0)
    empty_source_count: int = Field(..., ge=0)
    snapshot_count: int = Field(..., ge=0)
    unique_product_count: int = Field(..., ge=0)
    duplicate_product_count: int = Field(..., ge=0)
    priced_product_count: int = Field(..., ge=0)
    comparable_price_count: int = Field(..., ge=0)
    warning_count: int = Field(..., ge=0)


class ProductComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    comparison_version: str = CAPABILITY_VERSION
    summary: ComparisonSummary
    sources: list[SourceCoverage]
    products: list[ProductComparisonGroup]
    warnings: list[ComparisonWarning] = Field(default_factory=list)


_SELECT = """
SELECT snapshot_id, execution_trace_id, source_trace_id, source_artifact_id,
       source_content_hash, normalized_product_id, source_product_id,
       product_payload
FROM collection.product_snapshots
WHERE execution_trace_id = ANY(%s::uuid[])
ORDER BY execution_trace_id::text, source_artifact_id::text,
         normalized_product_id, snapshot_id
"""


def _connect(db: postgresql):
    return psycopg2.connect(
        host=db["host"],
        port=db.get("port", 5432),
        dbname=db["dbname"],
        user=db["user"],
        password=db["password"],
        sslmode=db.get("sslmode", "disable"),
    )


def load_snapshot_rows(db: postgresql, requests: list[SnapshotSourceRequest]) -> list[SnapshotRow]:
    if not requests:
        return []
    conn = _connect(db)
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(_SELECT, ([str(item.execution_trace_id) for item in requests],))
            names = [column.name for column in cursor.description]
            return [
                SnapshotRow.model_validate(dict(zip(names, values)))
                for values in cursor.fetchall()
            ]
    finally:
        conn.close()


def _token(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def product_match_key(product: NormalizedProduct) -> str:
    """Stable identity priority: GTIN, brand+MPN, brand+SKU, URL, brand+name, id."""
    identifiers = product.identifiers
    brand = _token(product.brand)
    if identifiers.gtin:
        return f"gtin:{identifiers.gtin}"
    if identifiers.mpn and brand:
        return f"brand_mpn:{brand}|{_token(identifiers.mpn)}"
    if identifiers.sku and brand:
        return f"brand_sku:{brand}|{_token(identifiers.sku)}"
    if product.canonical_url:
        return f"url:{product.canonical_url.casefold()}"
    if brand or product.name:
        return f"brand_name:{brand}|{_token(product.name)}"
    return f"normalized_id:{product.normalized_product_id}"


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _prices(product: NormalizedProduct) -> dict[str, str]:
    lowest: dict[str, Decimal] = {}
    for offer in product.offers:
        if (
            offer.price_status is not ValueStatus.valid
            or offer.currency_status is not CurrencyStatus.valid
            or offer.amount is None
            or offer.currency is None
        ):
            continue
        amount = Decimal(offer.amount)
        lowest[offer.currency] = min(lowest.get(offer.currency, amount), amount)
    return {currency: _decimal_text(lowest[currency]) for currency in sorted(lowest)}


def _observation(row: SnapshotRow) -> ProductObservation:
    product = row.product_payload
    return ProductObservation(
        snapshot_id=row.snapshot_id,
        execution_trace_id=row.execution_trace_id,
        source_artifact_id=row.source_artifact_id,
        normalized_product_id=row.normalized_product_id,
        source_product_id=row.source_product_id,
        name=product.name,
        brand=product.brand,
        sku=product.identifiers.sku,
        gtin=product.identifiers.gtin,
        mpn=product.identifiers.mpn,
        canonical_url=product.canonical_url,
        prices=_prices(product),
    )


def _price_differences(observations: list[ProductObservation]) -> list[PriceDifference]:
    by_currency: dict[str, list[SourcePrice]] = defaultdict(list)
    for observation in observations:
        for currency, amount in observation.prices.items():
            by_currency[currency].append(SourcePrice(
                source_artifact_id=observation.source_artifact_id,
                normalized_product_id=observation.normalized_product_id,
                amount=amount,
            ))
    comparisons = []
    for currency in sorted(by_currency):
        values = sorted(
            by_currency[currency],
            key=lambda item: (Decimal(item.amount), str(item.source_artifact_id)),
        )
        if len(values) < 2:
            continue
        minimum = Decimal(values[0].amount)
        maximum = Decimal(values[-1].amount)
        difference = maximum - minimum
        percentage = None if minimum == 0 else difference / minimum * Decimal("100")
        comparisons.append(PriceDifference(
            currency=currency,
            values=values,
            minimum=_decimal_text(minimum),
            maximum=_decimal_text(maximum),
            absolute_difference=_decimal_text(difference),
            percentage_difference=(
                None if percentage is None else _decimal_text(percentage.quantize(Decimal("0.01")))
            ),
        ))
    return comparisons


def compare_snapshot_rows(
    rows: list[SnapshotRow | dict[str, Any]],
    requested_sources: list[SnapshotSourceRequest | dict[str, Any]],
) -> ProductComparisonResult:
    """Pure deterministic comparison; callers provide rows loaded from any store."""
    snapshots = sorted(
        (SnapshotRow.model_validate(row) for row in rows),
        key=lambda row: (
            str(row.execution_trace_id), str(row.source_artifact_id),
            row.normalized_product_id, row.snapshot_id,
        ),
    )
    requests = [SnapshotSourceRequest.model_validate(item) for item in requested_sources]
    labels = {item.execution_trace_id: item.label for item in requests}
    requested_ids = {item.execution_trace_id for item in requests}
    unexpected = {row.execution_trace_id for row in snapshots} - requested_ids
    if unexpected:
        raise ValueError("snapshot rows include execution traces that were not requested")

    warnings: list[ComparisonWarning] = []
    source_rows: dict[tuple[UUID, UUID], list[SnapshotRow]] = defaultdict(list)
    for row in snapshots:
        source_rows[(row.execution_trace_id, row.source_artifact_id)].append(row)

    sources: list[SourceCoverage] = []
    retained: list[SnapshotRow] = []
    represented_executions = set()
    duplicate_count = 0
    for (execution_id, artifact_id), items in sorted(
        source_rows.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    ):
        represented_executions.add(execution_id)
        seen = set()
        unique = []
        duplicates = 0
        for row in items:
            key = product_match_key(row.product_payload)
            if key in seen:
                duplicates += 1
                duplicate_count += 1
                warnings.append(ComparisonWarning(
                    code="duplicate_product",
                    message=f"duplicate match key {key} ignored within source",
                    execution_trace_id=execution_id,
                    source_artifact_id=artifact_id,
                    normalized_product_id=row.normalized_product_id,
                ))
                continue
            seen.add(key)
            unique.append(row)
        retained.extend(unique)
        priced = sum(bool(_prices(row.product_payload)) for row in unique)
        sources.append(SourceCoverage(
            execution_trace_id=execution_id,
            source_trace_id=items[0].source_trace_id,
            source_artifact_id=artifact_id,
            source_content_hash=items[0].source_content_hash,
            label=labels.get(execution_id),
            product_count=len(items),
            unique_product_count=len(unique),
            priced_product_count=priced,
            duplicate_product_count=duplicates,
        ))
    for request in requests:
        if request.execution_trace_id not in represented_executions:
            sources.append(SourceCoverage(
                execution_trace_id=request.execution_trace_id,
                label=request.label,
                product_count=0,
                unique_product_count=0,
                priced_product_count=0,
                duplicate_product_count=0,
            ))
            warnings.append(ComparisonWarning(
                code="empty_source",
                message="requested execution has no persisted product snapshots",
                execution_trace_id=request.execution_trace_id,
            ))
    sources.sort(key=lambda source: (
        str(source.execution_trace_id), str(source.source_artifact_id or "")
    ))

    grouped: dict[str, list[ProductObservation]] = defaultdict(list)
    for row in retained:
        observation = _observation(row)
        grouped[product_match_key(row.product_payload)].append(observation)
        if not observation.prices:
            warnings.append(ComparisonWarning(
                code="missing_comparable_price",
                message="product has no offer with both a valid amount and currency",
                execution_trace_id=row.execution_trace_id,
                source_artifact_id=row.source_artifact_id,
                normalized_product_id=row.normalized_product_id,
            ))

    products = []
    comparable_price_count = 0
    for key in sorted(grouped):
        observations = sorted(
            grouped[key], key=lambda item: (
                str(item.source_artifact_id), item.normalized_product_id
            )
        )
        currencies = {currency for item in observations for currency in item.prices}
        if len(currencies) > 1:
            warnings.append(ComparisonWarning(
                code="currency_mismatch",
                message="matched product uses multiple currencies; prices are compared per currency",
            ))
        differences = _price_differences(observations)
        comparable_price_count += len(differences)
        products.append(ProductComparisonGroup(
            match_key=key,
            display_name=sorted(
                (item.name for item in observations), key=lambda value: value.casefold()
            )[0],
            observations=observations,
            price_differences=differences,
        ))

    warnings.sort(key=lambda warning: (
        warning.code,
        str(warning.execution_trace_id or ""),
        str(warning.source_artifact_id or ""),
        warning.normalized_product_id or "",
    ))
    covered = sum(source.source_artifact_id is not None for source in sources)
    priced = sum(source.priced_product_count for source in sources)
    summary = ComparisonSummary(
        requested_source_count=len(requests),
        covered_source_count=covered,
        empty_source_count=sum(source.product_count == 0 for source in sources),
        snapshot_count=len(snapshots),
        unique_product_count=len(products),
        duplicate_product_count=duplicate_count,
        priced_product_count=priced,
        comparable_price_count=comparable_price_count,
        warning_count=len(warnings),
    )
    return ProductComparisonResult(
        summary=summary,
        sources=sources,
        products=products,
        warnings=warnings,
    )


def compare_from_database(
    db: postgresql,
    requested_sources: list[SnapshotSourceRequest | dict[str, Any]],
) -> ProductComparisonResult:
    requests = [SnapshotSourceRequest.model_validate(item) for item in requested_sources]
    return compare_snapshot_rows(load_snapshot_rows(db, requests), requests)


def main(db: postgresql, requested_sources: list[dict]) -> dict:
    return compare_from_database(db, requested_sources).model_dump(mode="json")
