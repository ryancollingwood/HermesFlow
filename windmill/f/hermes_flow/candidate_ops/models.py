"""
Candidate record model and path/id generation — path: f/hermes_flow/candidate_ops/models

Other scripts import these directly:

    from f.hermes_flow.candidate_ops.models import CandidateRecord, compute_candidate_id, compute_candidate_path

(same import pattern as `f.hermes_flow.catalogue.models`.) `CandidateRecord`
is the metadata HF-011's `create` operation persists alongside every
candidate it writes under `f/hermes_flow/candidates/` — reason, provenance
(`source_path`/`base_version` when derived from an active capability),
conversation/request references, and what generated it.

This module deliberately lives under `f/hermes_flow/candidate_ops/`, a
sibling of `f/hermes_flow/candidates/` rather than inside it: the latter is
the runtime-only, git-excluded namespace `CANDIDATES_ROOT` points at (see
`wmill.yaml`'s `excludes`) — putting this module's own *code* inside the
very directory that's excluded from sync would mean it never syncs either.

`compute_candidate_id()` is deterministic: the same `request_key` always
produces the same id, and therefore the same path
(`compute_candidate_path()`). This is what makes candidate creation
idempotent (HF-011's own acceptance criterion) without needing a separate
ledger — the candidate's own existence at its deterministic path *is* the
idempotency check (see `f.hermes_flow.candidate_ops.create`).

Schema versioning follows the same additive-only-within-a-MAJOR rule as
`f.libraries.lineage.models` — see that module's docstring.
"""
import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"
CANDIDATES_ROOT = "f/hermes_flow/candidates"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_candidate_id(request_key: str) -> str:
    """Deterministic short id: same request_key -> same id, always."""
    if not request_key or not request_key.strip():
        raise ValueError("request_key must not be empty")
    return hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:16]


def compute_candidate_path(candidate_id: str) -> str:
    return f"{CANDIDATES_ROOT}/{candidate_id}"


def metadata_variable_path(candidate_id: str) -> str:
    """Where this candidate's CandidateRecord is persisted, as a plain (non-secret)
    Windmill variable — candidate metadata isn't a credential, just structured data
    that doesn't belong inside the script's own source."""
    return f"{CANDIDATES_ROOT}/{candidate_id}_metadata"


class CandidateRecord(BaseModel):
    """Metadata for one candidate: why it exists, what it's derived from (if
    anything), and what/who asked for it."""

    schema_version: str = Field(default=SCHEMA_VERSION)
    candidate_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    request_key: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, description="Why this candidate was created.")
    source_path: Optional[str] = Field(
        default=None,
        description="Active capability path this candidate was derived from, if any.",
    )
    base_version: Optional[str] = Field(
        default=None,
        description="source_path's Windmill script hash at the time of derivation — lets "
        "a later promotion check detect the base has since changed (stale-base conflict, "
        "HF-013). Required whenever source_path is set.",
    )
    conversation_id: Optional[str] = Field(default=None)
    request_id: Optional[str] = Field(default=None)
    generated_by_capability: Optional[str] = Field(
        default=None,
        description="Capability path that generated this candidate's content, if an AI "
        "capability (HF-019) produced it rather than a human.",
    )
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _derived_candidates_need_a_base_version(self) -> "CandidateRecord":
        if self.source_path is not None and not self.base_version:
            raise ValueError("base_version is required whenever source_path is set")
        return self

    @model_validator(mode="after")
    def _path_matches_candidate_id(self) -> "CandidateRecord":
        expected = compute_candidate_path(self.candidate_id)
        if self.path != expected:
            raise ValueError(f"path {self.path!r} does not match candidate_id {self.candidate_id!r} "
                              f"(expected {expected!r}) — candidate paths must be deterministic")
        return self
