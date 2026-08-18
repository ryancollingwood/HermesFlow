"""HF-027 bounded end-to-end product collection workflow implementation."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypedDict

import jsonschema  # noqa: F401 -- required by imported structured-output validation
import psycopg2  # noqa: F401 -- required by imported persistence/comparison capabilities
from f.capabilities.collection.compare_product_snapshots import (
    ProductComparisonResult,
    compare_from_database,
)
from f.capabilities.collection.extract_products import extract_products
from f.capabilities.collection.normalise_products import normalise_products
from f.capabilities.collection.product_snapshot_write import (
    ProductSnapshotWriteResult,
    persist_product_snapshots,
)
from f.capabilities.collection.render_product_report import store_product_report
from f.capabilities.collection.web_fetch import web_fetch
from f.hermes.client import hermes_endpoint
from f.libraries.lineage.helpers import (
    LineageState,
    begin_lineage,
    child_context,
    write_artifact,
)
from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext
from f.libraries.results.models import (
    ArtifactSummary,
    ExecutionResult,
    ExecutionType,
    ResultOutcome,
    WindmillJobRef,
)
from f.libraries.storage.artifacts import FilesystemArtifactStore
from pydantic import BaseModel, ConfigDict, Field, field_validator

WORKFLOW_PATH = "f/workflows/product_collection"
WORKFLOW_VERSION = "1.0.0"
MAX_SOURCES = 20
MAX_CONCURRENCY = 8

CAPABILITY_VERSIONS = {
    "f/capabilities/collection/web_fetch": "1.0.0",
    "f/capabilities/collection/extract_products": "1.0.0",
    "f/capabilities/collection/normalise_products": "1.0.0",
    "f/capabilities/collection/product_snapshot_write": "1.0.0",
    "f/capabilities/collection/compare_product_snapshots": "1.0.0",
    "f/capabilities/collection/render_product_report": "1.0.0",
    WORKFLOW_PATH: WORKFLOW_VERSION,
}


class postgresql(TypedDict):
    host: str
    port: int
    user: str
    dbname: str
    password: str
    sslmode: str


class ProductSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=1)
    allowed_domains: list[str] = Field(..., min_length=1, max_length=20)
    source_type: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enable_ai_fallback: bool | None = None


class SourceStatus(str, Enum):
    success = "success"
    empty = "empty"
    failed = "failed"


class SourceRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    label: str
    url: str
    status: SourceStatus
    fetch_trace_id: str
    persistence_trace_id: str
    raw_artifact: ArtifactRef | None = None
    normalized_artifact: ArtifactRef | None = None
    extraction_method: str | None = None
    product_count: int = Field(default=0, ge=0)
    persistence: ProductSnapshotWriteResult | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class ProductCollectionWorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    workflow_version: str = WORKFLOW_VERSION
    status: str
    max_concurrency: int
    ai_fallback_enabled: bool
    capability_versions: dict[str, str]
    sources: list[SourceRunResult]
    comparison: ProductComparisonResult | None = None
    dataset_artifact: ArtifactRef | None = None
    report_artifact: ArtifactRef | None = None
    execution_result: ExecutionResult
    lineage: LineageState


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[ProductSource] = Field(..., min_length=1, max_length=MAX_SOURCES)
    max_concurrency: int = Field(default=4, ge=1, le=MAX_CONCURRENCY)
    enable_ai_fallback: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_size_bytes: int = Field(default=5_000_000, ge=1, le=100_000_000)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_products_per_source: int = Field(default=100, ge=1, le=1000)

    @field_validator("sources")
    @classmethod
    def _source_ids_are_unique(cls, value: list[ProductSource]) -> list[ProductSource]:
        ids = [source.source_id for source in value]
        duplicates = sorted({source_id for source_id in ids if ids.count(source_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate source_id values: {duplicates}")
        return value


@dataclass(frozen=True)
class _SourcePlan:
    index: int
    source: ProductSource
    fetch_context: ExecutionContext
    normalize_context: ExecutionContext
    persistence_context: ExecutionContext


def _artifact_summaries(source_results: list[SourceRunResult]) -> list[ArtifactSummary]:
    summaries = []
    for source in source_results:
        for artifact, description in (
            (source.raw_artifact, f"Raw source: {source.label}"),
            (source.normalized_artifact, f"Normalized products: {source.label}"),
        ):
            if artifact is not None:
                summaries.append(ArtifactSummary(
                    artifact_id=artifact.artifact_id,
                    stage=artifact.stage,
                    storage_uri=artifact.storage_uri,
                    description=description,
                ))
    return summaries


def _deduplicate_artifact_summaries(items: list[ArtifactSummary]) -> list[ArtifactSummary]:
    seen = set()
    result = []
    for item in items:
        if item.artifact_id not in seen:
            seen.add(item.artifact_id)
            result.append(item)
    return result


def _process_source(
    plan: _SourcePlan,
    *,
    state: LineageState,
    store: FilesystemArtifactStore,
    db: postgresql,
    hermes_conn: dict | None,
    workflow_input: WorkflowInput,
    fetcher: Callable,
    extractor: Callable,
    normalizer: Callable,
    persister: Callable,
) -> SourceRunResult:
    source = plan.source
    raw_artifact = None
    normalized_artifact = None
    extraction_method = None
    product_count = 0
    persistence = None
    warnings = []
    try:
        fetched = fetcher(
            source.url,
            source.allowed_domains,
            headers=source.headers,
            timeout_seconds=workflow_input.timeout_seconds,
            max_size_bytes=workflow_input.max_size_bytes,
            max_retries=workflow_input.max_retries,
            context=plan.fetch_context,
            lineage=state,
            store=store,
        )
        raw_artifact = fetched.raw_artifact
        if fetched.status != "success" or raw_artifact is None:
            raise RuntimeError(fetched.error or f"fetch ended with status {fetched.status}")
        source_ai = (
            workflow_input.enable_ai_fallback
            if source.enable_ai_fallback is None
            else source.enable_ai_fallback
        )
        extraction = extractor(
            raw_artifact,
            source_metadata={
                "source_url": fetched.final_url or source.url,
                "source_type": source.source_type,
            },
            lineage=state,
            store=store,
            ai_conn=hermes_conn if source_ai else None,
            max_products=workflow_input.max_products_per_source,
        )
        extraction_method = extraction.method
        warnings.extend(f"{item.code}: {item.message}" for item in extraction.warnings)
        normalization = normalizer(extraction)
        product_count = len(normalization.products)
        warnings.extend(normalization.warnings)
        normalized_artifact = write_artifact(
            state,
            store,
            plan.normalize_context,
            json.dumps(
                normalization.model_dump(mode="json"),
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            stage=ArtifactStage.intermediate,
            media_type="application/json",
            inputs=[raw_artifact],
            metadata={
                "kind": "normalized_product_source",
                "source_id": source.source_id,
                "schema_version": normalization.schema_version,
            },
        )
        persistence = persister(
            normalization,
            plan.persistence_context,
            db,
        )
        return SourceRunResult(
            source_id=source.source_id,
            label=source.label,
            url=source.url,
            status=SourceStatus.success if product_count else SourceStatus.empty,
            fetch_trace_id=str(plan.fetch_context.trace_id),
            persistence_trace_id=str(plan.persistence_context.trace_id),
            raw_artifact=raw_artifact,
            normalized_artifact=normalized_artifact,
            extraction_method=extraction_method,
            product_count=product_count,
            persistence=persistence,
            warnings=warnings,
        )
    except Exception as exc:
        return SourceRunResult(
            source_id=source.source_id,
            label=source.label,
            url=source.url,
            status=SourceStatus.failed,
            fetch_trace_id=str(plan.fetch_context.trace_id),
            persistence_trace_id=str(plan.persistence_context.trace_id),
            raw_artifact=raw_artifact,
            normalized_artifact=normalized_artifact,
            extraction_method=extraction_method,
            product_count=product_count,
            persistence=persistence,
            warnings=warnings,
            error=str(exc),
        )


def run_product_collection(
    sources: list[dict[str, Any] | ProductSource],
    db: postgresql,
    *,
    hermes_conn: dict | None = None,
    enable_ai_fallback: bool = False,
    max_concurrency: int = 4,
    timeout_seconds: float = 30,
    max_size_bytes: int = 5_000_000,
    max_retries: int = 2,
    max_products_per_source: int = 100,
    job_id: str | None = None,
    workspace: str = "main",
    store: FilesystemArtifactStore | None = None,
    fetcher: Callable = web_fetch,
    extractor: Callable = extract_products,
    normalizer: Callable = normalise_products,
    persister: Callable = persist_product_snapshots,
    comparator: Callable = compare_from_database,
    reporter: Callable = store_product_report,
) -> ProductCollectionWorkflowResult:
    started = time.monotonic()
    workflow_input = WorkflowInput(
        sources=sources,
        max_concurrency=max_concurrency,
        enable_ai_fallback=enable_ai_fallback,
        timeout_seconds=timeout_seconds,
        max_size_bytes=max_size_bytes,
        max_retries=max_retries,
        max_products_per_source=max_products_per_source,
    )
    ai_fallback_enabled = any(
        source.enable_ai_fallback is True
        or (source.enable_ai_fallback is None and workflow_input.enable_ai_fallback)
        for source in workflow_input.sources
    )
    if ai_fallback_enabled and not hermes_conn:
        raise ValueError("Hermes connection is required when AI fallback is enabled")
    resolved_job_id = job_id or os.environ.get("WM_JOB_ID")
    if not resolved_job_id:
        raise ValueError("Windmill job ID is required")
    state, root_context = begin_lineage(
        capability=WORKFLOW_PATH,
        capability_version=WORKFLOW_VERSION,
        initiating_actor="windmill",
        request_id=resolved_job_id,
    )
    artifact_store = store or FilesystemArtifactStore(max_size_bytes=max_size_bytes)
    plans = []
    for index, source in enumerate(workflow_input.sources):
        plans.append(_SourcePlan(
            index=index,
            source=source,
            fetch_context=child_context(
                state, root_context,
                capability="f/capabilities/collection/web_fetch",
                capability_version=CAPABILITY_VERSIONS["f/capabilities/collection/web_fetch"],
            ),
            normalize_context=child_context(
                state, root_context,
                capability="f/capabilities/collection/normalise_products",
                capability_version=CAPABILITY_VERSIONS["f/capabilities/collection/normalise_products"],
            ),
            persistence_context=child_context(
                state, root_context,
                capability="f/capabilities/collection/product_snapshot_write",
                capability_version=CAPABILITY_VERSIONS["f/capabilities/collection/product_snapshot_write"],
            ),
        ))
    report_context = child_context(
        state, root_context,
        capability="f/capabilities/collection/render_product_report",
        capability_version=CAPABILITY_VERSIONS["f/capabilities/collection/render_product_report"],
    )
    completed: dict[int, SourceRunResult] = {}
    with ThreadPoolExecutor(max_workers=workflow_input.max_concurrency) as pool:
        futures = {
            pool.submit(
                _process_source,
                plan,
                state=state,
                store=artifact_store,
                db=db,
                hermes_conn=hermes_conn,
                workflow_input=workflow_input,
                fetcher=fetcher,
                extractor=extractor,
                normalizer=normalizer,
                persister=persister,
            ): plan.index
            for plan in plans
        }
        for future in as_completed(futures):
            completed[futures[future]] = future.result()
    source_results = [completed[index] for index in range(len(plans))]
    requests = [
        {
            "execution_trace_id": source.persistence_trace_id,
            "label": source.label,
        }
        for source in source_results
    ]
    job_ref = WindmillJobRef(
        job_id=resolved_job_id,
        workspace=workspace,
        path=WORKFLOW_PATH,
    )
    source_artifacts = _artifact_summaries(source_results)
    source_failures = [source for source in source_results if source.status is SourceStatus.failed]
    try:
        comparison = comparator(db, requests)
        report = reporter(
            comparison,
            state,
            report_context,
            job_ref,
            store=artifact_store,
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:
        execution_result = ExecutionResult(
            outcome=ResultOutcome.failure,
            execution_type=ExecutionType.windmill_job,
            workflow_path=WORKFLOW_PATH,
            capability_version=WORKFLOW_VERSION,
            job=job_ref,
            duration_seconds=time.monotonic() - started,
            artifacts=source_artifacts,
            warnings=[
                f"source {source.source_id}: {source.error}" for source in source_failures
            ],
            failure_summary=f"comparison or reporting failed: {exc}",
        )
        return ProductCollectionWorkflowResult(
            status="failure",
            max_concurrency=workflow_input.max_concurrency,
            ai_fallback_enabled=ai_fallback_enabled,
            capability_versions=CAPABILITY_VERSIONS,
            sources=source_results,
            execution_result=execution_result,
            lineage=state,
        )

    comparison_warnings = [
        f"{warning.code}: {warning.message}" for warning in comparison.warnings
    ]
    warnings = [
        f"source {source.source_id}: {source.error}" for source in source_failures
    ] + comparison_warnings
    completed_sources = len(source_results) - len(source_failures)
    if completed_sources == 0:
        outcome = ResultOutcome.failure
        status = "failure"
        failure_summary = "all product sources failed; review per-source errors"
    elif source_failures or comparison_warnings:
        outcome = ResultOutcome.partial
        status = "partial"
        failure_summary = None
    else:
        outcome = ResultOutcome.success
        status = "success"
        failure_summary = None
    artifacts = _deduplicate_artifact_summaries(
        source_artifacts + report.execution_result.artifacts
    )
    execution_result = ExecutionResult(
        outcome=outcome,
        execution_type=ExecutionType.windmill_job,
        workflow_path=WORKFLOW_PATH,
        capability_version=WORKFLOW_VERSION,
        job=job_ref,
        duration_seconds=time.monotonic() - started,
        artifacts=artifacts,
        warnings=warnings,
        failure_summary=failure_summary,
    )
    return ProductCollectionWorkflowResult(
        status=status,
        max_concurrency=workflow_input.max_concurrency,
        ai_fallback_enabled=ai_fallback_enabled,
        capability_versions=CAPABILITY_VERSIONS,
        sources=source_results,
        comparison=comparison,
        dataset_artifact=report.dataset_artifact,
        report_artifact=report.report_artifact,
        execution_result=execution_result,
        lineage=report.lineage,
    )


def main(
    sources: list[dict],
    db: postgresql,
    hermes_conn: hermes_endpoint = {},
    enable_ai_fallback: bool = False,
    max_concurrency: int = 4,
    timeout_seconds: float = 30,
    max_size_bytes: int = 5_000_000,
    max_retries: int = 2,
    max_products_per_source: int = 100,
) -> dict:
    return run_product_collection(
        sources,
        db,
        hermes_conn=hermes_conn or None,
        enable_ai_fallback=enable_ai_fallback,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        max_size_bytes=max_size_bytes,
        max_retries=max_retries,
        max_products_per_source=max_products_per_source,
    ).model_dump(mode="json")
