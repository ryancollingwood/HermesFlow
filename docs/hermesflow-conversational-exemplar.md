# Conversational product collection exemplar

HF-028 teaches the `hermesflow` skill to turn a natural-language product
research request into the HF-027 Windmill workflow without bypassing capability
search or execution policy.

## Conversation contract

Hermes classifies the requested outcome before execution:

| Intent | Behavior |
|---|---|
| One-off collection, product research, or comparison from explicit URLs | Search and inspect Windmill, evaluate `execute`, then run automatically when within policy and bounds |
| Missing URLs, unclear output, or “watch” language that could imply scheduling | Search first, ask one focused clarification, and do not execute |
| Purchase, add-to-cart, price/catalogue mutation, deletion, or other source-system write | Search for an approved capability, fail closed when none exists, and do not reinterpret the request as a report |

Every request triggers discovery through Windmill's `listFlows` or
`listScripts`; an asset remembered from an earlier turn is not treated as a
search result. Hermes inspects the selected schema with `getFlowByPath` before
constructing arguments. The current end-to-end match is
`f/workflows/product_collection` version `1.0.0`.

Run `make hermesflow-mcp` once on the live stack. It registers a stdio MCP
server with one public tool and mints/reuses `HF_PRODUCT_MCP_TOKEN` with only
`jobs:run` and `jobs:read`. The broader REST run scope is isolated inside this
fixed-flow server; the model can neither choose another path nor schedule work.

For supported reads, each URL becomes one narrowly allowlisted source. Hostnames
are derived exactly from the requested URLs, AI fallback remains off unless
explicitly requested, and workflow bounds are never silently exceeded. The
active flow's internal PostgreSQL snapshots and artifact writes support lineage;
they do not authorize mutation of a retailer or source system.

## Result contract

Hermes explains the selected workflow in one sentence without dumping internal
HF-021–HF-026 implementation paths. It submits the inspected flow through the
narrow `run_product_collection` MCP tool, then waits for the Windmill result and
reports:

- success, partial, or failure and actionable warnings;
- workflow path and version;
- Windmill workspace and job ID; and
- returned raw, normalized, dataset, and report artifact references.

No submitted job means Hermes cannot claim execution succeeded. Missing artifact
or version values are reported as missing rather than invented.

## Evaluation

The deterministic prompt set at
`windmill/tests/fixtures/hermesflow_conversation_prompts.yaml` covers direct
match, varied sources, ambiguous scheduling language, and an unsupported write.
`windmill/tests/test_hermesflow_conversation_skill.py` runs those prompts through
the real HF-009 search and HF-010 policy code and validates the skill contract.

An opt-in live test exercises Hermes → Windmill → Hermes after the skill,
workflow, database migration, and narrow execution tool are deployed:

```sh
make collection-db-migrate windmill-push hermesflow-mcp
HF_RUN_HERMES_LIVE=1 pytest -q \
  windmill/tests/test_hermesflow_conversation_skill.py::test_live_hermes_to_windmill_and_back
```

It uses a toolset-scoped Hermes session and the approved `example.com` source;
CI leaves it skipped because CI does not run the full Docker stack or carry live
Windmill credentials.
