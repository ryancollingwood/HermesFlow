# Failure inspection and repair context

HF-029 adds `f/hermes_flow/repair/inspection`, the read-only Windmill operation
that turns one failed job into a versioned `RepairContext`. It is evidence for a
later repair decision, not permission to patch or retry the active capability.

## Evidence contract

The inspector starts from the original failed job and returns:

- an API link to that job, including workspace, path, and job ID;
- the active capability version and a bounded code/flow snapshot;
- the active script's immutable Windmill hash for HF-030 stale-base checks;
- bounded, redacted job inputs and logs;
- artifact references discovered in the failed result, never artifact contents;
- direct and transitive consumer impact from the versioned catalogue;
- recent test evidence that explicitly declares the failed capability; and
- a deterministic classification with reasons and confidence.

The checked-in JSON Schema is
[`docs/schemas/repair_context.schema.json`](schemas/repair_context.schema.json).
`test_failure_inspection.py` fails when the model and schema drift.

## Classification

The classifier recognizes `input`, `source_drift`, `code_defect`, `dependency`,
`policy`, and `infrastructure`. Evidence without a supported deterministic signal
is `unknown`; the inspector does not guess a root cause. Specific policy, input,
source, dependency, and infrastructure signals take precedence over a generic
traceback. For example, a PostgreSQL `UndefinedTable` traceback is a missing
schema dependency, while a refused database connection is infrastructure.

Classification is advisory evidence for HF-030. It does not automatically
select retry, repair, rollback, or promotion.

HF-030 consumes a complete context as documented in
[`docs/repair-candidate-generation.md`](repair-candidate-generation.md). A
truncated or stale active-code snapshot is not eligible for generation.

## Bounds and omissions

The default serialized result limit is 128 KiB. Code, inputs, and logs have
independent byte budgets; artifacts, dependency impacts, and tests have count
budgets. When evidence exceeds a section budget, the inspector retains bounded
head/tail context and a SHA-256 digest of the complete redacted section. If the
whole envelope still exceeds its limit, it progressively reduces documents and
then list tails until the exact serialized-size check passes.

`truncation` records every truncated section and omitted-list count. A repair
consumer can therefore distinguish “no evidence existed” from “evidence existed
but was outside the configured bound.”

## Privacy and credential handling

Sensitive input keys and credential-shaped text are replaced with
`[REDACTED]` before hashing, sizing, or classification. Coverage includes API
keys, passwords, tokens, authorization/cookie headers, credential-bearing URLs,
common provider tokens, and private keys. The replacement count is returned.

Conversation, messages, prompts, user profiles, and memory fields are excluded
rather than redacted because they are not repair evidence. Their field paths are
listed in `redaction.excluded_fields`. Test evidence is filtered by declared
capability path, so unrelated tests and their details are not retained.

## Windmill use

The operation requires the failed job ID and the text of
`windmill/capability-index.yaml`; Windmill jobs cannot read the checked-out repo,
so the caller supplies that versioned catalogue. Optional test evidence is also
caller-supplied until a centralized test-evidence store exists.

```python
main(
    job_id="019f...",
    catalogue_yaml="schema_version: '1.0'\nentries: ...",
    recent_test_evidence=[],
    windmill_base_url="http://windmill.localhost",
    max_total_bytes=131072,
)
```

If job logs or active code are unavailable, the context remains linked to the
job and records a collection warning. If the original job itself cannot be
retrieved, inspection fails instead of fabricating a context.
