"""Versioned models for HF-029 bounded failure-inspection contexts."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


SCHEMA_VERSION = "1.0"


class FailureCategory(str, Enum):
    input = "input"
    source_drift = "source_drift"
    code_defect = "code_defect"
    dependency = "dependency"
    policy = "policy"
    infrastructure = "infrastructure"
    unknown = "unknown"


class RepairContextLimits(BaseModel):
    max_total_bytes: int = Field(default=131_072, ge=16_384, le=2_000_000)
    max_code_bytes: int = Field(default=48_000, ge=256, le=500_000)
    max_input_bytes: int = Field(default=16_000, ge=256, le=250_000)
    max_log_bytes: int = Field(default=32_000, ge=256, le=500_000)
    max_artifacts: int = Field(default=20, ge=0, le=100)
    max_dependencies: int = Field(default=50, ge=0, le=250)
    max_test_evidence: int = Field(default=20, ge=0, le=100)


class BoundedDocument(BaseModel):
    content: str
    original_bytes: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)
    truncated: bool = False
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OriginalJob(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=200)
    workspace: str = Field(..., min_length=1, max_length=200)
    path: str = Field(..., min_length=1, max_length=500)
    api_url: str = Field(..., min_length=1, max_length=2000)


class ActiveCapabilityEvidence(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    capability_version: Optional[str] = Field(default=None, max_length=200)
    windmill_hash: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Windmill's immutable active-script hash captured during inspection.",
    )
    asset_kind: str = Field(default="script", max_length=20)
    code: BoundedDocument


class FailureClassification(BaseModel):
    category: FailureCategory
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list, max_length=10)


class ArtifactEvidence(BaseModel):
    artifact_id: Optional[str] = Field(default=None, max_length=200)
    stage: Optional[str] = Field(default=None, max_length=100)
    storage_uri: Optional[str] = Field(default=None, max_length=2000)
    description: Optional[str] = Field(default=None, max_length=1000)


class DependencyEvidence(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    relationship: str = Field(..., min_length=1, max_length=50)
    distance: int = Field(ge=1)
    via: Optional[str] = Field(default=None, max_length=500)
    tests: list[str] = Field(default_factory=list, max_length=50)


class RecentTestEvidence(BaseModel):
    test: str = Field(..., min_length=1, max_length=500)
    status: str = Field(..., min_length=1, max_length=50)
    job_id: Optional[str] = Field(default=None, max_length=200)
    recorded_at: Optional[str] = Field(default=None, max_length=100)
    details: Optional[str] = Field(default=None, max_length=2000)


class RedactionSummary(BaseModel):
    replacement_count: int = Field(default=0, ge=0)
    excluded_fields: list[str] = Field(default_factory=list, max_length=50)


class TruncationSummary(BaseModel):
    truncated_sections: list[str] = Field(default_factory=list)
    omitted_artifacts: int = Field(default=0, ge=0)
    omitted_dependencies: int = Field(default=0, ge=0)
    omitted_test_evidence: int = Field(default=0, ge=0)


class RepairContext(BaseModel):
    schema_version: str = SCHEMA_VERSION
    original_job: OriginalJob
    failure_summary: str = Field(..., min_length=1, max_length=4000)
    classification: FailureClassification
    active_capability: ActiveCapabilityEvidence
    inputs: BoundedDocument
    logs: BoundedDocument
    artifacts: list[ArtifactEvidence] = Field(default_factory=list)
    dependency_impact: list[DependencyEvidence] = Field(default_factory=list)
    recent_test_evidence: list[RecentTestEvidence] = Field(default_factory=list)
    redaction: RedactionSummary
    truncation: TruncationSummary
    collection_warnings: list[str] = Field(default_factory=list, max_length=20)
    total_bytes: int = Field(default=0, ge=0)
    limits: RepairContextLimits

    def serialized_size(self) -> int:
        return len(self.model_dump_json().encode("utf-8"))


def main() -> dict[str, Any]:
    return {"RepairContext": RepairContext.model_json_schema()}
