"""
Retention classes, expiry, and cost-estimation helpers — path:
f/libraries/retention/models

Other scripts import these directly:

    from f.libraries.retention.models import RetentionPolicy, is_expired, select_expired

(same import pattern as `f.libraries.lineage.models`.) HF-035 documents,
rather than invents, a retention posture for the five data categories the
lifecycle backlog names — job logs, prompts, conversations, raw artifacts,
and datasets — and gives `f.hermes_flow.policies.evaluator` the size/
duration/record-count/cost limit vocabulary it needs
(`f.libraries.capability.models.CapabilityLimits`) to enforce declared
per-capability bounds the same deterministic way it already enforces
concurrency and rate.

What this module is NOT: a scheduler, a garbage collector, or a pricing
service. `select_expired()` only *selects* which already-known items are
past their retention window; the caller (a future Windmill schedule, not
introduced here) is responsible for actually deleting them — for artifacts,
via `f.libraries.storage.artifacts.FilesystemArtifactStore.delete`, which
writes a tombstone (`f.libraries.lineage.models.ArtifactTombstone`) so
`derived_from` lineage chains stay resolvable after the content itself is
gone. `estimate_cost_usd()` is a caller-supplied price table applied to a
usage dict (e.g. HF-019's `InvocationResult.usage`); it does not fetch or
cache real provider pricing.

Secrets and credentials are excluded from retained artifacts at the point
they are written, not here: HF-019's `invoke_hermes_structured._redact`
strips them from prompt/conversation/raw-response artifacts before they
ever reach the artifact store, and HF-029's
`f.hermes_flow.repair.inspection.redact_text`/`_sanitize` do the same for
retained failure-inspection logs/inputs. This module's job is retention
*duration and deletion*, not redaction.

Schema versioning follows the same additive-only-within-a-MAJOR rule as
`f.libraries.lineage.models` — see that module's docstring.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class RetentionClass(str, Enum):
    """How long a data category is kept, independent of *why* (see RetentionPolicy.notes)."""

    ephemeral = "ephemeral"
    """Never persisted past the request that produced it (e.g. an in-memory rate-limit
    counter) — no expiry to select, nothing to delete."""

    short_term = "short_term"
    """Kept for an operational debugging/troubleshooting window, then deleted."""

    standard = "standard"
    """The default working set for artifacts still likely to be referenced by ongoing
    work (repair, review, audit) — kept longer than short_term, still bounded."""

    long_term = "long_term"
    """Kept indefinitely unless explicitly superseded or deleted (e.g. a dataset that
    downstream reports depend on) — `max_age_seconds` is typically unset."""

    indefinite = "indefinite"
    """Never auto-expired by this module at all; deletion is always a deliberate,
    separately-authorised action (e.g. compliance/audit records)."""


class RetentionPolicy(BaseModel):
    """Declared retention bounds for one data category."""

    schema_version: str = Field(default=SCHEMA_VERSION)
    category: str = Field(..., min_length=1)
    retention_class: RetentionClass
    max_age_seconds: int | None = Field(
        default=None, gt=0,
        description="Delete once an item is older than this. Unset means no automatic "
        "age-based expiry (typical for long_term/indefinite).",
    )
    max_size_bytes: int | None = Field(default=None, gt=0)
    max_record_count: int | None = Field(default=None, gt=0)
    requires_tombstone: bool = Field(
        default=True,
        description="Whether deletion of this category must preserve a tombstone record "
        "(lineage-linked artifacts) rather than a bare delete.",
    )
    notes: str = Field(default="", max_length=2000)


# Documented default policy per data category named in the HF-035 acceptance
# criteria. A capability or operator may declare stricter limits (via
# CapabilityLimits) for its own artifacts; these are the repo-wide defaults,
# not a hard ceiling enforced by this module itself.
DEFAULT_RETENTION_POLICIES: dict[str, RetentionPolicy] = {
    "job_logs": RetentionPolicy(
        category="job_logs",
        retention_class=RetentionClass.short_term,
        max_age_seconds=30 * 86_400,
        requires_tombstone=False,
        notes="Windmill's own job log store, not a HermesFlow artifact — retained for "
        "operational debugging only. No lineage chain references a job log, so a bare "
        "expiry (no tombstone) is sufficient.",
    ),
    "prompts": RetentionPolicy(
        category="prompts",
        retention_class=RetentionClass.standard,
        max_age_seconds=180 * 86_400,
        requires_tombstone=True,
        notes="HF-019 prompt/raw-response artifacts. Redacted by "
        "invoke_hermes_structured._redact before they are ever written to the artifact "
        "store — this policy governs how long the redacted artifact is kept, not whether "
        "secrets reach it.",
    ),
    "conversations": RetentionPolicy(
        category="conversations",
        retention_class=RetentionClass.standard,
        max_age_seconds=180 * 86_400,
        requires_tombstone=True,
        notes="Conversation transcripts referenced by ExecutionContext.conversation_id. "
        "Redacted the same way as prompts before retention.",
    ),
    "raw_artifacts": RetentionPolicy(
        category="raw_artifacts",
        retention_class=RetentionClass.short_term,
        max_age_seconds=30 * 86_400,
        requires_tombstone=True,
        notes="HF-017 raw-stage artifacts (page scrapes, un-normalised model output). "
        "Short-lived because intermediate/final artifacts derived from them are what "
        "downstream work actually depends on; the raw input itself is debugging evidence.",
    ),
    "datasets": RetentionPolicy(
        category="datasets",
        retention_class=RetentionClass.long_term,
        max_age_seconds=None,
        requires_tombstone=True,
        notes="data_platform mart/dataset outputs (docs/plans/datalake.md). Retained "
        "indefinitely by default since downstream reports depend on them; deletion is "
        "always an explicit, tombstone-preserving action, never age-based.",
    ),
}


def is_expired(policy: RetentionPolicy, age_seconds: float) -> bool:
    """An item with no age-based expiry (`max_age_seconds` unset) is never selected here."""
    if age_seconds < 0:
        raise ValueError("age_seconds must not be negative")
    return policy.max_age_seconds is not None and age_seconds >= policy.max_age_seconds


def select_expired(
    items: list[tuple[str, datetime]],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return the ids of `items` (id, created_at) whose age meets or exceeds
    `policy.max_age_seconds`. Pure selection — deletion is the caller's job."""
    if policy.max_age_seconds is None:
        return []
    current_time = now or datetime.now(timezone.utc)
    selected = []
    for item_id, created_at in items:
        if created_at.tzinfo is None:
            raise ValueError(f"created_at for {item_id!r} must be timezone-aware")
        age_seconds = (current_time - created_at).total_seconds()
        if is_expired(policy, max(0.0, age_seconds)):
            selected.append(item_id)
    return selected


def estimate_cost_usd(
    usage: dict,
    *,
    price_per_1k_prompt_tokens: float = 0.0,
    price_per_1k_completion_tokens: float = 0.0,
) -> float:
    """Estimate USD cost from a token-usage dict (e.g. HF-019's `InvocationResult.usage`,
    itself whatever the OpenAI-compatible SDK returned — `prompt_tokens`/`completion_tokens`
    fields are the common shape). Caller supplies the price table; this performs no
    provider lookup and caches nothing.
    """
    if price_per_1k_prompt_tokens < 0 or price_per_1k_completion_tokens < 0:
        raise ValueError("prices must not be negative")
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must not be negative")
    return (
        (prompt_tokens / 1000) * price_per_1k_prompt_tokens
        + (completion_tokens / 1000) * price_per_1k_completion_tokens
    )


def main() -> dict:
    """Self-test / demo: export the RetentionPolicy JSON Schema and the default table."""
    return {
        "RetentionPolicy": RetentionPolicy.model_json_schema(),
        "default_policies": {
            key: policy.model_dump(mode="json") for key, policy in DEFAULT_RETENTION_POLICIES.items()
        },
    }
