# Product collection exemplar

Use this contract for a natural-language request to research, collect, or
compare products across web sources.

## 1. Classify intent

- **Supported read:** collect product facts, compare prices or availability,
  or produce a one-off report from explicit web sources.
- **Ambiguous:** missing source URLs/domains; unclear product or output; words
  such as “watch” or “keep checking” that may mean a schedule.
- **Unsupported write:** purchase, add to cart, update a source catalogue,
  change a price, delete a product, or write back to an external store.

Search in every case. Search is side-effect-free and automatic. For ambiguity,
search first so the clarification is informed, then ask one focused question
and stop. For unsupported writes, explain that no matching approved capability
exists and do not reinterpret the request as a read-only report.

## 2. Search and inspect

1. Call `listFlows` with product/collection/comparison terms. Call
   `listScripts` too only when one primitive may cover the entire request.
2. Prefer the exact end-to-end match `f/workflows/product_collection` over
   generating or manually composing duplicate logic.
3. Call `getFlowByPath` for the selected path. Confirm its description, input
   schema, and declared bounds before forming arguments.

Describe the choice to the user as: “I’m using the product collection workflow
to fetch the approved sources, normalize comparable product data, persist
snapshots, and return a comparison report.” Do not list HF-021 through HF-026 or
their internal paths unless the user asks for implementation detail.

## 3. Resolve bounded arguments

For each explicit source URL:

- assign a stable `source_id` and concise label;
- set `url` exactly as requested; and
- derive `allowed_domains` from the URL hostname, lowercased and without a
  trailing dot. Never broaden it to a parent domain unless the user explicitly
  approved that domain.

Use conservative defaults unless the request needs tighter values:

```json
{
  "sources": [
    {
      "source_id": "store-a",
      "label": "Store A",
      "url": "https://store-a.example/products/item",
      "allowed_domains": ["store-a.example"]
    }
  ],
  "db": "$res:f/collection/collection_db",
  "enable_ai_fallback": false,
  "max_concurrency": 4,
  "timeout_seconds": 30,
  "max_size_bytes": 5000000,
  "max_retries": 2,
  "max_products_per_source": 100
}
```

Keep AI fallback false unless the user explicitly requests it and an approved
`hermes_endpoint` resource is available. A request for more than 20 sources,
concurrency above 8, or any other schema violation is denied rather than silently
clamped.

## 4. Apply policy and execute

Evaluate `execute` against the selected catalogue metadata with the requested
concurrency and `destructive=false` only for a supported one-off read. The
active workflow's automatic execute policy permits starting immediately within
bounds. Its database/filesystem effects retain snapshots and artifacts inside
HermesFlow; they do not authorize purchasing or changing the source system.

If policy returns `automatic`, call the `hermesflow` MCP tool
`run_product_collection` with the validated source and bound arguments, then
poll Windmill's `getJob` until completion. Native `runFlowByPath` cannot pass
arguments and must not be used for this flow. The narrow tool fixes the flow
path, database resource, and AI setting; it cannot schedule work or select an
arbitrary job target. Use `getJobLogs` only when the result fails or needs
diagnosis. If policy returns `approval_required`, explain the decision and wait.
If it returns `denied`, do not execute.

## 5. Present the result

Lead with success, partial, or failure. Include:

- `f/workflows/product_collection` and workflow version;
- the Windmill job ID/workspace;
- concise per-source failures or warnings when partial;
- raw, normalized, comparison dataset, and report artifact references returned
  by the workflow; and
- the actionable `failure_summary` when failed.

Never claim the workflow ran if no job was submitted, and never manufacture
artifact URLs or version values that are absent from the returned result.
