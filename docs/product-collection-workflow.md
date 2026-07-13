# Product collection workflow

HF-027 composes the HF-021 through HF-026 product capabilities into the Windmill
flow `f/workflows/product_collection`. The flow is the only execution boundary:
fetching, extraction, normalization, persistence, comparison, and report creation
all run inside its Windmill worker job.

## Inputs and bounds

Each source requires a stable `source_id`, label, HTTP(S) URL, and non-empty
`allowed_domains` list. A run accepts 1–20 sources and 1–8 concurrent source
workers. Fetch time, response bytes, retries, and products per source are also
hard-bounded by the input model and Windmill JSON Schema.

Deterministic extraction is always attempted first. AI fallback is off by
default and is used only when `enable_ai_fallback` (globally or on a source) is
explicitly true and a `hermes_endpoint` resource is supplied. Enabling it without
that resource fails before the run starts.

## Failure and artifact behavior

Every source has its own fetch, normalization, and persistence contexts. A source
failure is captured in that source's result without cancelling successful peers;
any raw or normalized artifact written before the failure remains linked. A run
is `partial` when at least one source succeeds and another fails, and `failure`
when every source fails. Comparison/report failures also produce a failure
envelope retaining all source artifacts created so far.

The final result includes:

- ordered per-source status, errors, warnings, artifact references, and snapshot
  write dispositions;
- comparison dataset and Markdown report artifacts;
- the workflow and every composed capability version;
- the complete HF-018 lineage state; and
- a standard HF-019 `ExecutionResult` containing the Windmill job ID, workspace,
  flow path, outcome, duration, warnings, and artifact links.

HF-025's key `(execution_trace_id, source_artifact_id,
normalized_product_id)` makes a retry of the same persistence step idempotent:
unchanged rows are reported rather than duplicated.

## Verification

`windmill/tests/test_product_collection_workflow.py` runs Shopify, Next.js, and
OpenGraph fixtures end to end against disposable PostgreSQL. It covers bounded
parallelism, one-source isolation, total failure, idempotent persistence retry,
explicit AI policy, input bounds, result schema, flow metadata, and lineage. The
scheduled live manifest remains opt-in and bounded to an explicitly approved
test domain.
