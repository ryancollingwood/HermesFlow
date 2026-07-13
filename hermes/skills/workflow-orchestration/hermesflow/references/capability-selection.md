# Capability selection

How to apply SKILL.md's Rule 2 (primitives → workflows → generation) given
what's actually built in this repo today, and how to read a capability's
metadata before deciding to reuse it.

## Search order

1. **Primitive** — one narrowly-scoped capability that already does the
   job. Prefer this over anything composed; it's the smallest possible
   surface to review, test, and reason about.
2. **Workflow** — an existing Windmill flow that composes primitives into
   an end-to-end result covering the request, even if no single primitive
   does.
3. **Generate** — write new code, only once 1 and 2 are genuinely
   exhausted. See `generation-policy.md` for what that requires.

Don't skip straight to 3 because searching feels slower than writing —
that's exactly the shortcut this ordering exists to prevent. If you're
unsure whether something existing covers the request, that uncertainty is
itself a reason to search harder before generating, not a reason to
generate "to be safe."

## How to search, today

The versioned catalogue now lives at `windmill/capability-index.yaml`, with
validated entries from HF-008 and deterministic ranking in
`f/hermes_flow/catalogue/search` from HF-009. The ranked operation accepts the
catalogue YAML as input because Windmill jobs cannot read the checked-out repo.
When that input is available in the calling context, use the ranked operation
and preserve its rationale.

A restricted HermesFlow conversation deliberately has no filesystem tool, so
its MCP discovery path is:

- Call `listScripts` / `listFlows` via the `windmill` MCP toolset. Calling at
  least one of these is required before selection; recalling a path from an
  earlier turn is not a search.
- Use task keywords, expected input/output shape, and summaries to shortlist
  assets in primitives → workflows order. For an end-to-end product collection
  result, `output_kinds=product_collection_workflow_result` identifies the
  catalogue match even though the MCP list surface exposes summary rather than
  the repo-only catalogue record.
- Inspect every shortlisted asset with `getScriptByPath` / `getFlowByPath`.
  The returned schema is authoritative for arguments and bounds.
- `searchDocs` (Windmill's own doc search, exposed as an MCP tool) if you
  need to understand a Windmill feature rather than find a capability.
- Read the selected catalogue entry's `CapabilityMetadata` where it is present;
  for legacy assets without an entry, treat their summary/docstring only as
  discovery text and fail closed for execution policy.
- Asking the user directly when the above doesn't turn up a clear answer,
  rather than guessing and generating something that duplicates an
  existing but hard-to-find capability.

## Reading `CapabilityMetadata` before reusing something

Where a `CapabilityMetadata` record exists for a candidate capability
(`f/libraries/capability/models.py`), check before treating it as usable:

- **`maturity`** — `experimental` capabilities may not have proven
  themselves; weigh that against how consequential the task is.
  `deprecated` means look for what replaced it instead of using it.
- **`effects`** — `network`/`filesystem`/`database`/`external` tell you
  what this capability actually touches when it runs. A capability with
  `database=True` writing somewhere you didn't expect is a signal to read
  its `summary` more carefully, not a reason to avoid it outright.
- **`autonomy`** — per-action `AutonomyPolicy` tells you what's automatic
  versus what needs approval for *this specific capability*.
  `execute=automatic` (the default for reviewed, active capabilities) means
  routine execution doesn't need a human in the loop; `promote`/`schedule`
  are always `approval_required`, structurally, for every capability — see
  SKILL.md Rule 3.
- **`dependencies`** — other capability paths this one relies on. Once
  [HF-012](https://github.com/ryancollingwood/HermesFlow/issues/50)'s
  impact analysis exists this feeds consumer traversal; until then, treat
  it as a manual "check these still exist and still do what this expects"
  list before relying on the capability.
- **`limits`** — `timeout_seconds`/`max_concurrency`/`rate_limit_per_minute`
  bound what the capability will tolerate. Don't drive it outside those
  bounds and treat the resulting failure as a bug in the capability.

## Two worked examples

From `windmill/tests/test_capability_models.py`, the shape of what a
`CapabilityMetadata` record looks like for each end of the risk spectrum:

- **Read-only**: `f/capabilities/collection/web_fetch` —
  `effects.network=True`, everything else `False`; `execute=automatic`.
  Safe to reuse freely within its declared `limits`.
- **Write**: `f/capabilities/collection/product_snapshot_write` —
  `effects.database=True`; still `execute=automatic` (routine execution of
  already-active, already-reviewed code isn't what's gated — see SKILL.md
  Rule 3), but its `promote`/`schedule` are `approval_required` like every
  other capability.
