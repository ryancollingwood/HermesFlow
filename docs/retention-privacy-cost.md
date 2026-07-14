# Retention, privacy, and cost controls

HF-035 documents retention for the five data categories the lifecycle
backlog names — job logs, prompts, conversations, raw artifacts, and
datasets — and gives `f.hermes_flow.policies.evaluator` the size/duration/
record-count/cost limit vocabulary it needs to enforce declared
per-capability bounds. It is a policy and schema layer over existing
mechanisms (HF-013's evaluator, HF-017's artifact store, HF-019/HF-029's
redaction), not a new scheduler or garbage collector.

## Retention classes

`f/libraries/retention/models.py` defines `RetentionClass` (`ephemeral`,
`short_term`, `standard`, `long_term`, `indefinite`) and a `RetentionPolicy`
per data category. `DEFAULT_RETENTION_POLICIES` is the repo-wide default
table:

| Category | Retention class | Default window | Tombstone required? |
|---|---|---|---|
| `job_logs` | `short_term` | 30 days | no — Windmill's own job log store, no lineage chain references it |
| `prompts` | `standard` | 180 days | yes |
| `conversations` | `standard` | 180 days | yes |
| `raw_artifacts` | `short_term` | 30 days | yes |
| `datasets` | `long_term` | none (indefinite by default) | yes |

A capability may declare a stricter bound for its own artifacts via
`CapabilityLimits`; this table is the default posture, not a hard ceiling
this module enforces on its own.

`is_expired(policy, age_seconds)` and `select_expired(items, policy, now=...)`
are pure selection helpers — they decide *which* already-known
`(id, created_at)` pairs are past their window. Neither deletes anything;
for artifacts, the caller pairs a `select_expired` result with
`FilesystemArtifactStore.delete` (see below). A policy with no
`max_age_seconds` (`long_term`/`indefinite` categories, by default) never
selects anything — deletion for those stays a deliberate, separately
authorised action.

## Size, duration, record-count, and model-cost limits

`f.libraries.capability.models.CapabilityLimits` already had
`timeout_seconds` (duration), `max_concurrency`, and `rate_limit_per_minute`.
HF-035 adds the other three legs of the same vocabulary:

- `max_response_bytes` — largest response/output size, in bytes.
- `max_record_count` — largest number of records one invocation may
  produce or process.
- `max_cost_usd` — largest estimated model/inference cost, in USD.

`f.hermes_flow.policies.evaluator.evaluate_policy` enforces all four the
same deterministic way it already enforced concurrency/rate: a
`PolicyContext.requested_*` value that exceeds the capability's own declared
limit is `denied`, never routed to approval. A declared limit with no
matching `requested_*` value isn't a violation — there's nothing to compare,
exactly like the pre-existing concurrency/rate behaviour. Duration reuses
the pre-existing `timeout_seconds` field rather than introducing a
duplicate.

Cost enforcement is caller-estimated, not looked up from a live pricing
service: `f.libraries.retention.models.estimate_cost_usd(usage,
price_per_1k_prompt_tokens=..., price_per_1k_completion_tokens=...)` turns a
token-usage dict (the shape HF-019's `invoke_hermes_structured` already
returns) into a USD estimate the caller then supplies as
`PolicyContext.requested_cost_usd`. This module holds no pricing table of
its own and caches nothing.

## Deletion preserves tombstone lineage

`FilesystemArtifactStore.delete(ref, reason)` (HF-035) replaces an
artifact's metadata envelope with an `ArtifactTombstone`
(`f.libraries.lineage.models`) carrying the same `artifact_id`, `trace_id`,
`stage`, `content_hash`, and `derived_from` as the deleted `ArtifactRef`,
plus `reason` and `deleted_at`. A downstream artifact's `derived_from` link
still resolves to a real (if tombstoned) record after deletion — nothing is
silently orphaned. `read()`/`read_text()` raise a dedicated
`ArtifactTombstonedError` for a tombstoned artifact rather than a generic
"missing object" error, so callers can tell "deliberately retention-deleted"
apart from "never existed" or "corrupt". Deletion is idempotent: deleting an
already-tombstoned `artifact_id` again returns the existing tombstone
unchanged.

`delete()` deliberately does **not** remove the shared content-addressed
object at `content_hash` itself. The store deduplicates identical bytes
across `artifact_id`s (see `write()`'s existing behaviour), and without a
reference count there is no way to know another, still-retained
`artifact_id` isn't relying on the same bytes — removing them on one
artifact's expiry could silently destroy a different, still-live artifact.
The metadata this artifact_id's own lineage/retention record depends on
(including any caller-supplied `metadata` dict, which may itself carry
sensitive context) is what deletion actually removes; physical
reference-counted garbage collection of the underlying object store is
future work, not something this task introduces.

## Secrets and credentials are excluded before retention, not after

This module governs retention *duration and deletion*; redaction happens
where artifacts are written, before HF-035 or the artifact store ever see
them:

- `f.libraries.ai.invoke_hermes_structured._redact` strips sensitive-shaped
  keys and configured secret values from every prompt, conversation, input,
  and raw-response artifact HF-019 retains.
- `f.hermes_flow.repair.inspection.redact_text`/`_sanitize` do the
  equivalent for HF-029's retained failure-inspection logs, inputs, and
  code snapshots.

Nothing in HF-035 re-implements or bypasses either — the retention windows
and deletion mechanism above apply to already-redacted content.

## Enforcing limits where supported

"Where supported" is deliberate: not every capability can measure every
dimension before it runs. A capability that can estimate its own response
size, record count, or (for AI-backed capabilities) cost before or during
execution should supply the corresponding `PolicyContext.requested_*` value
so `evaluate_policy` can enforce it; one that can't simply omits it, exactly
as `requested_concurrency`/`requested_rate_per_minute` already work today.

Machine-readable contracts are checked in at
`docs/schemas/retention_policy.schema.json` and
`docs/schemas/artifact_tombstone.schema.json`.
