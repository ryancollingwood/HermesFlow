"""
Execution context and artifact reference schemas — path: f/libraries/lineage/models

Other scripts import these directly:

    from f.libraries.lineage.models import ExecutionContext, ArtifactRef, ArtifactStage

(same import pattern as `f.hermes.client`.) Every workflow run carries one
`ExecutionContext`; every raw/intermediate/final output it produces is
described by an `ArtifactRef` pointing back at that context's trace and,
where applicable, at the artifacts it was derived from.

Schema versioning: each model carries `schema_version` (MAJOR.MINOR, as a
string). Within a MAJOR version, only additive, optional fields may be
introduced — never remove, rename, or narrow a required field, and never
change a field's meaning. That keeps an older reader forward-compatible with
newer-but-same-MAJOR payloads: extra fields it doesn't know about are simply
ignored (Pydantic's default `model_config` behaviour). A MAJOR bump signals a
breaking change and readers must not assume compatibility across one.

Running THIS script directly exports both models' JSON Schemas, which
doubles as a self-test — see `docs/schemas/` for the checked-in copies used
by docs/CI (`windmill/tests/test_lineage_models.py` asserts they match).
"""
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactStage(str, Enum):
    """Where an artifact sits in a run's raw -> intermediate -> final pipeline."""

    raw = "raw"
    intermediate = "intermediate"
    final = "final"


class ExecutionContext(BaseModel):
    """Identifies one workflow/capability execution and who/what initiated it."""

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema MAJOR.MINOR this record was written against.",
    )
    trace_id: UUID = Field(
        default_factory=uuid4,
        description="Unique ID for this execution.",
    )
    parent_trace_id: UUID | None = Field(
        default=None,
        description="trace_id of the execution that spawned this one, if any "
        "(e.g. a flow step's sub-run). Absent for a top-level execution.",
    )
    conversation_id: str | None = Field(
        default=None,
        description="Hermes conversation/session this execution was requested from, if any.",
    )
    request_id: str | None = Field(
        default=None,
        description="Caller-supplied idempotency/correlation key for this specific request, if any.",
    )
    capability: str = Field(
        ...,
        min_length=1,
        description="Capability path that is executing, e.g. f/capabilities/collection/web_fetch.",
    )
    capability_version: str = Field(
        ...,
        min_length=1,
        description="Version of that capability that is executing.",
    )
    initiating_actor: str = Field(
        ...,
        min_length=1,
        description="Who/what initiated this execution: a user id, 'hermes', a schedule name, etc.",
    )
    started_at: datetime = Field(
        default_factory=_utcnow,
        description="When this execution began (UTC).",
    )

    @field_validator("parent_trace_id")
    @classmethod
    def _parent_must_differ_from_self(
        cls, v: UUID | None, info
    ) -> UUID | None:
        trace_id = info.data.get("trace_id")
        if v is not None and trace_id is not None and v == trace_id:
            raise ValueError(
                "parent_trace_id must not equal trace_id — an execution cannot be its own parent"
            )
        return v


class ArtifactRef(BaseModel):
    """Points at one stored artifact and how it fits into a run's lineage."""

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema MAJOR.MINOR this record was written against.",
    )
    artifact_id: UUID = Field(
        default_factory=uuid4,
        description="Unique ID for this artifact reference.",
    )
    trace_id: UUID = Field(
        ...,
        description="trace_id of the ExecutionContext that produced this artifact.",
    )
    stage: ArtifactStage = Field(
        ...,
        description="raw (untransformed input), intermediate (a transformation step's "
        "output), or final (a run's end result).",
    )
    content_hash: str = Field(
        ...,
        description="Lowercase 64-character hex SHA-256 digest of the artifact's content.",
    )
    storage_uri: str = Field(
        ...,
        min_length=1,
        description="Where the content lives, e.g. "
        "file:///shared/artifacts/<hash[:2]>/<hash>.",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Artifact content size in bytes. HF-017 storage adapters always populate it; "
        "optional for backward compatibility with pre-HF-017 references.",
    )
    media_type: str | None = Field(
        default=None,
        min_length=1,
        description="IANA media type of the stored content. HF-017 adapters always populate it; "
        "optional for backward compatibility with pre-HF-017 references.",
    )
    creator_capability: str = Field(
        ...,
        min_length=1,
        description="Capability path that produced this artifact.",
    )
    creator_capability_version: str = Field(
        ...,
        min_length=1,
        description="Version of that capability at the time it produced this artifact.",
    )
    derived_from: list[UUID] = Field(
        default_factory=list,
        description="artifact_id of each artifact this one was derived from. Empty for "
        "a raw artifact with no upstream input captured by this store.",
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        description="When this artifact was written (UTC).",
    )

    @field_validator("content_hash")
    @classmethod
    def _hash_must_be_sha256_hex(cls, v: str) -> str:
        v = v.lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError(
                "content_hash must be a 64-character lowercase hex SHA-256 digest"
            )
        return v

    @field_validator("derived_from")
    @classmethod
    def _cannot_derive_from_self(
        cls, v: list[UUID], info
    ) -> list[UUID]:
        artifact_id = info.data.get("artifact_id")
        if artifact_id is not None and artifact_id in v:
            raise ValueError("derived_from must not include this artifact's own artifact_id")
        return v


class ArtifactTombstone(BaseModel):
    """HF-035: what remains of an `ArtifactRef` after its content is deleted.

    Deleting an artifact's bytes must not break `derived_from` lineage chains
    that point at it — a downstream artifact's `derived_from` list stays
    resolvable (the id still exists, just as a tombstone) even though the
    content itself is gone. See
    `f.libraries.storage.artifacts.FilesystemArtifactStore.delete`.
    """

    schema_version: str = Field(default=SCHEMA_VERSION)
    artifact_id: UUID = Field(..., description="Same artifact_id as the deleted ArtifactRef.")
    trace_id: UUID
    stage: ArtifactStage
    content_hash: str = Field(
        ...,
        description="The deleted content's hash, retained for lineage/audit even though the "
        "object itself is gone.",
    )
    creator_capability: str = Field(..., min_length=1)
    creator_capability_version: str = Field(..., min_length=1)
    derived_from: list[UUID] = Field(default_factory=list)
    reason: str = Field(..., min_length=1, description="Why this artifact was deleted.")
    deleted_at: datetime = Field(default_factory=_utcnow)


def main() -> dict:
    """Self-test / demo: export all three models' JSON Schemas."""
    return {
        "ExecutionContext": ExecutionContext.model_json_schema(),
        "ArtifactRef": ArtifactRef.model_json_schema(),
        "ArtifactTombstone": ArtifactTombstone.model_json_schema(),
    }
