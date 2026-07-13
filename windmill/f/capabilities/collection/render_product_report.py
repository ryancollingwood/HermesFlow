"""HF-026 human-readable product report and result-envelope artifact writer."""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict

from f.capabilities.collection.compare_product_snapshots import ProductComparisonResult
from f.libraries.lineage.helpers import LineageState, write_artifact
from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext
from f.libraries.results.models import (
    ArtifactSummary,
    ExecutionResult,
    ExecutionType,
    ResultOutcome,
    WindmillJobRef,
)
from f.libraries.storage.artifacts import FilesystemArtifactStore

CAPABILITY_PATH = "f/capabilities/collection/render_product_report"
CAPABILITY_VERSION = "1.0.0"


class ProductReportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    status: str
    comparison: ProductComparisonResult
    report_text: str
    dataset_artifact: ArtifactRef
    report_artifact: ArtifactRef
    execution_result: ExecutionResult
    lineage: LineageState


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_product_report(comparison: ProductComparisonResult | dict) -> str:
    result = ProductComparisonResult.model_validate(comparison)
    summary = result.summary
    lines = [
        "# Product comparison report",
        "",
        "## Summary",
        "",
        f"- Source coverage: {summary.covered_source_count}/{summary.requested_source_count} "
        f"({summary.empty_source_count} empty)",
        f"- Snapshots read: {summary.snapshot_count}",
        f"- Unique products: {summary.unique_product_count}",
        f"- Duplicate products ignored: {summary.duplicate_product_count}",
        f"- Products with comparable price data: {summary.priced_product_count}",
        f"- Same-currency price comparisons: {summary.comparable_price_count}",
        f"- Warnings: {summary.warning_count}",
        "",
        "## Source coverage",
        "",
        "| Source | Execution trace | Source artifact | Products | Unique | Priced | Duplicates |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for source in result.sources:
        lines.append(
            f"| {_cell(source.label or 'unlabelled')} | `{source.execution_trace_id}` | "
            f"{f'`{source.source_artifact_id}`' if source.source_artifact_id else '—'} | "
            f"{source.product_count} | {source.unique_product_count} | "
            f"{source.priced_product_count} | {source.duplicate_product_count} |"
        )
    if not result.sources:
        lines.append("| — | — | — | 0 | 0 | 0 | 0 |")

    lines.extend(["", "## Product comparisons", ""])
    if not result.products:
        lines.extend(["_No products were available for comparison._", ""])
    for group in result.products:
        lines.extend([
            f"### {_cell(group.display_name)}",
            "",
            f"- Match key: `{_cell(group.match_key)}`",
            f"- Source observations: {len(group.observations)}",
            "",
            "| Source artifact | Product ID | Prices |",
            "|---|---|---|",
        ])
        for observation in group.observations:
            prices = ", ".join(
                f"{currency} {amount}" for currency, amount in observation.prices.items()
            ) or "—"
            lines.append(
                f"| `{observation.source_artifact_id}` | "
                f"`{observation.normalized_product_id}` | {_cell(prices)} |"
            )
        lines.extend(["", "Price differences:", ""])
        if not group.price_differences:
            lines.append("- No same-currency price pair was available.")
        for difference in group.price_differences:
            percentage = (
                "n/a" if difference.percentage_difference is None
                else f"{difference.percentage_difference}%"
            )
            lines.append(
                f"- {difference.currency}: {difference.minimum} → {difference.maximum} "
                f"(Δ {difference.absolute_difference}, {percentage})"
            )
        lines.append("")

    lines.extend(["## Warnings", ""])
    if not result.warnings:
        lines.append("_No warnings._")
    for warning in result.warnings:
        reference = (
            f" (source artifact `{warning.source_artifact_id}`)"
            if warning.source_artifact_id else ""
        )
        lines.append(f"- `[{warning.code}]` {_cell(warning.message)}{reference}")
    return "\n".join(lines).rstrip() + "\n"


def _source_artifacts(
    comparison: ProductComparisonResult,
    lineage: LineageState,
) -> list[ArtifactRef]:
    artifacts = []
    seen = set()
    for source in comparison.sources:
        if source.source_artifact_id is None or source.source_artifact_id in seen:
            continue
        artifact = lineage.artifacts.get(source.source_artifact_id)
        if artifact is None:
            raise ValueError(
                f"source artifact {source.source_artifact_id} is absent from lineage"
            )
        if artifact.trace_id != source.source_trace_id:
            raise ValueError("source artifact trace does not match comparison coverage")
        if artifact.content_hash != source.source_content_hash:
            raise ValueError("source artifact hash does not match comparison coverage")
        seen.add(source.source_artifact_id)
        artifacts.append(artifact)
    return artifacts


def store_product_report(
    comparison: ProductComparisonResult | dict,
    lineage: LineageState | dict,
    execution_context: ExecutionContext | dict,
    job: WindmillJobRef | dict,
    *,
    store: Optional[FilesystemArtifactStore] = None,
    duration_seconds: Optional[float] = None,
) -> ProductReportResult:
    result = ProductComparisonResult.model_validate(comparison)
    state = LineageState.model_validate(lineage)
    context = ExecutionContext.model_validate(execution_context)
    job_ref = WindmillJobRef.model_validate(job)
    if state.contexts.get(context.trace_id) != context:
        raise ValueError("report execution context is absent from lineage")
    source_artifacts = _source_artifacts(result, state)
    artifact_store = store or FilesystemArtifactStore()
    dataset_content = json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    dataset_artifact = write_artifact(
        state,
        artifact_store,
        context,
        dataset_content,
        stage=ArtifactStage.intermediate,
        media_type="application/json",
        inputs=source_artifacts,
        metadata={"kind": "product_comparison_dataset", "schema_version": result.schema_version},
    )
    report_text = render_product_report(result)
    report_artifact = write_artifact(
        state,
        artifact_store,
        context,
        report_text,
        stage=ArtifactStage.final,
        media_type="text/markdown; charset=utf-8",
        inputs=[dataset_artifact],
        metadata={"kind": "product_comparison_report", "schema_version": "1.0"},
    )
    warning_messages = [f"{warning.code}: {warning.message}" for warning in result.warnings]
    partial = bool(warning_messages) or not result.products
    execution_result = ExecutionResult(
        outcome=ResultOutcome.partial if partial else ResultOutcome.success,
        execution_type=ExecutionType.windmill_job,
        workflow_path=CAPABILITY_PATH,
        capability_version=CAPABILITY_VERSION,
        job=job_ref,
        duration_seconds=duration_seconds,
        artifacts=[
            ArtifactSummary(
                artifact_id=dataset_artifact.artifact_id,
                stage=dataset_artifact.stage,
                storage_uri=dataset_artifact.storage_uri,
                description="Machine-readable product comparison dataset",
            ),
            ArtifactSummary(
                artifact_id=report_artifact.artifact_id,
                stage=report_artifact.stage,
                storage_uri=report_artifact.storage_uri,
                description="Human-readable product comparison report",
            ),
        ],
        warnings=warning_messages,
    )
    return ProductReportResult(
        status="partial" if partial else "success",
        comparison=result,
        report_text=report_text,
        dataset_artifact=dataset_artifact,
        report_artifact=report_artifact,
        execution_result=execution_result,
        lineage=state,
    )


def main(
    comparison: dict,
    lineage_json: str,
    execution_context: dict,
    job: dict,
    duration_seconds: float = 0.0,
) -> dict:
    return store_product_report(
        comparison,
        LineageState.from_json(lineage_json),
        execution_context,
        job,
        duration_seconds=duration_seconds,
    ).model_dump(mode="json")
