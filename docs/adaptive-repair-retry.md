# Adaptive repair and retry

HF-032 provides `f/hermes_flow/repair/adaptive_repair`, the approval-gated
orchestrator for a failed active Python capability. It composes the existing
HermesFlow lifecycle rather than bypassing it:

1. reserve one atomic attempt record under
   `f/hermes_flow_state/adaptive_repair/`;
2. inspect and classify the failed job with HF-029;
3. stop before generation unless the classification is source drift, code
   defect, or dependency failure and `modify_candidate` policy permits it;
4. generate an isolated HF-030 candidate;
5. for source drift, sanitise the retained HTML/JSON artifact into an HF-031
   regression fixture;
6. run the candidate's required tests and all direct/transitive consumer
   contract or smoke tests selected by HF-016;
7. prepare the HF-013 diff, impact, required-test, and fixture evidence and
   suspend for authenticated native Windmill approval;
8. recheck the active base version, promote, and retry the original script once.

No active write occurs before approval. A denied classification, rejected
generation, missing drift fixture contract, failed or skipped test, rejected
approval, or stale active hash is terminal for that attempt.

## Retry and lineage contract

`f/hermes_flow/repair/finalize_retry` reloads the original job after promotion.
Its argument payload is held only in memory, copied into the retry request, and
never included in the flow response or non-secret attempt variable. The
finalizer replaces the configured context argument with a fresh
`ExecutionContext`; its `parent_trace_id` is the original execution's
`trace_id`, and its capability version is the promoted Windmill hash.

The returned `RetryRecord` links the failed job, repair candidate, promoted
version, retry job, original parent trace, and retry trace. It retains only a
SHA-256 digest of the retry result, not the result payload or original inputs.

## Loop bound and state

`max_attempts` accepts 1–3 and defaults to 2. Each reservation uses a
deterministic per-job/per-attempt variable created atomically. Existing slots
are never reused; once all slots exist, `AttemptLimitExceeded` stops the flow.
The retry never invokes repair recursively, so a repeatedly failing promoted
version cannot produce an infinite repair loop.

These attempt records are runtime state and deliberately live in the
unsynchronised `f/hermes_flow_state/` sibling. The scripts and approval flow are
listed individually in `windmill/wmill.yaml`; candidates, fixtures, original
arguments, and attempt state remain outside mirror scope.

Machine-readable contracts are checked in at
`docs/schemas/repair_preparation.schema.json` and
`docs/schemas/adaptive_retry_record.schema.json`.
