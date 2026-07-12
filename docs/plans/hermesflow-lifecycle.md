# HermesFlow lifecycle — implementation task breakdown

Derived from *HermesFlow High Level Solution and Development Plan* (2026-07-12),
reconciled against the repo as it stands. The document's HF-001…HF-036 backlog is
sound; this plan grounds each task in what already exists here, flags the tasks
that are partially done, and adds the few pieces the document assumes but never
schedules. Task IDs match the source document so they can become GitHub issues
directly.

## How this repo changes the document's assumptions

| Document assumption | Repo reality | Effect on tasks |
|---|---|---|
| "Establish safe Windmill namespace and sync plan" (HF-007) | `windmill/wmill.yaml` is already narrowly scoped post-incident (2026-06-29); `make windmill-push/pull/check` and `docs/windmill-sync.md` exist | HF-007 shrinks to: define the four new namespaces, add them to `includes:` deliberately, document the candidate (unsynced) namespace |
| AI wrapper built from scratch (HF-019) | `f/hermes/client.py` already provides an OpenAI-compatible Hermes client + `hermes_endpoint` resource type | HF-019 extends the existing client with schema validation, artifact retention and metadata capture |
| Skill deployment "commands include the new skill" (HF-005) | `make hermes-skills-push` is additive over `hermes/skills/<category>/<skill>/` | HF-005 only needs the skill authored in the right place; deployment is free |
| Language unstated (JSON Schema *or* Pydantic) | All Windmill assets are Python; `defaultTs: bun` in wmill.yaml is only the CLI default | Use **Pydantic** models as source of truth; export JSON Schema for docs/CI |
| "existing collection database" (HF-025) | `collection_db/` Postgres + `f/collection/collection_db.resource.yaml` exist | HF-025 targets that database; migrations live under `collection_db/` |
| Hermes ↔ lifecycle controls "via MCP" (§4 diagram) | **Already wired, undocumented**: `hermes mcp list` shows a live, enabled `windmill` MCP connection (native Streamable-HTTP, no bridge), token created 2026-06-21 — no ADR, no Makefile target, no mention anywhere in the repo | New task **HF-000B** below — document the existing wiring, prove it live, and flag it for productization |
| "content-addressed mounted artifact filesystem" (HF-017) | No artifact volume is defined in compose | New task **HF-000A** below |

## Added tasks (gaps in the document)

### HF-000A — Provision the artifact store volume
**Phase 1.** HF-017 assumes a mounted filesystem exists. Add a named volume (or
`data/artifacts/` bind mount, consistent with the existing `data/` layout) to
`docker-compose.yml`, mounted into the Windmill workers, with permissions
handled by `fix-permissions`/`install.py` the same way other data dirs are.
*Exit:* a Windmill script can write and re-read a file under the artifact root
across container restarts.

### HF-000B — Decide and wire the Hermes → Windmill invocation transport
**Phase 1 (ADR).** The document's diagram says "MCP" but never tasks it.
Initial framing (before investigation) favoured a bespoke HTTP-API tool over
MCP, on the assumption MCP connectivity had been flaky here before. Two facts
changed that: (1) Hermes has no generic HTTP-tool mechanism — every external
service it reaches (Baserow, Directus) is wired in as MCP, so "avoid MCP"
meant building unproven new plumbing instead of reusing the pattern already
in use; (2) the past MCP pain was specifically about bridging legacy SSE-only
services via `mcp-remote` (the `baserow-mcp` pattern) — Windmill CE ships a
**native** Streamable-HTTP/SSE MCP server (`/api/mcp/w/{workspace}/sse`, ~38
curated `x-mcp-tool` operations), the same zero-bridge case as Directus.

**Further discovery: it was already wired up** — `docker exec hermes hermes
mcp list` showed `windmill` registered and enabled, token dated 2026-06-21,
predating this plan entirely and undocumented anywhere in the repo.

**Decision: native MCP**, recorded as
`architecture/adr/0005-hermes-windmill-transport.md`, along with a live proof
run and the limitation it surfaced (see below). Everything in Phase 2
(catalogue search, policy evaluation, candidate ops) is exposed *through*
this transport.
*Exit (met):* `hermes chat -Q -t windmill -q "..."` listed `f/hermes`'s
scripts and submitted a real job (`019f55ef-e96c-a968-3e94-5610d732b37b`) for
`f/hermes/client` via `runScriptByPath`. The job itself failed — MCP's
`runScriptByPath` schema doesn't pass script arguments, so a script requiring
a resource-typed arg (`conn: hermes_endpoint`) got `None` instead — which is
a real, load-bearing constraint on capability design (see the ADR's
Consequences), not a transport failure.

**Follow-up (not yet scheduled as an issue):** this MCP wiring is live but
reproduced nowhere — no Makefile target creates it, so a fresh install
wouldn't have it, and the existing token (`mcp:all`, unscoped to workspace)
is broader than the bounded-autonomy model this plan is building toward.
Productize it as an idempotent `make windmill-mcp` target (mint/reuse a
narrowly-scoped token via Windmill's own token-scoping API, register with
Hermes, verify — same shape as `baserow-mcp`) before Phase 2 depends on it
more heavily.

## Sprint 1 — Execution contract and foundations (Phase 1)

| Task | Work in this repo | Notes |
|---|---|---|
| HF-001 Exclusive-execution ADR | Create `architecture/adr/0001-windmill-exclusive-execution.md`; link from README | New top-level dir; also seed ADRs 0002–0004 as stubs per §11 |
| HF-000B Transport ADR | `architecture/adr/0005-…`; prove one round trip | Blocks HF-005/006/008+ |
| HF-000A Artifact volume | compose + permissions | Blocks HF-017 |
| HF-002 Context & artifact schemas | Pydantic models in a shared Windmill module, e.g. `f/libraries/lineage/models.py`; schema_version field; unit tests | Importable via `from f.libraries.lineage.models import …` (same pattern as `f.hermes.client`) |
| HF-003 Capability metadata & autonomy schema | Pydantic models + two worked examples (read-only web capability, write capability); validation tests incl. "low-risk label ≠ promote permission" | Feeds `windmill/capability-index.yaml` |
| HF-004 Result envelope | Model + rendering guidance destined for the skill; snapshot tests for success/failure/partial | Failure rendering must always include Windmill job ref |
| HF-005 Orchestration skill skeleton | `hermes/skills/workflow-orchestration/hermesflow/SKILL.md` + `capability-selection.md`, `generation-policy.md`, `repair-policy.md`, `result-presentation.md` | Follows existing `<category>/<skill>/SKILL.md` + references convention; deployed by existing `make hermes-skills-push` |
| HF-006 Audit/restrict Hermes direct execution | Inventory Hermes's execution-capable tools; guard/scoping for HermesFlow sessions; clear Windmill-unavailable message | Test by attempting shell/python/browser/fs through HermesFlow mode |
| HF-007 Namespace & sync plan | Document namespaces `f/hermes_flow/`, `f/libraries/`, `f/capabilities/`, `f/workflows/`; add each to `wmill.yaml` `includes:` only as it gains content; candidates live at `f/hermes_flow/candidates/` and stay **out** of sync like `f/hermes_state` | Because candidates share the `f/hermes_flow/` root, `wmill.yaml` must enumerate included subpaths item-by-item (the `f/data_platform` pattern) — never `f/hermes_flow/**`. Update `docs/windmill-sync.md`; rehearse against a disposable workspace before first push — this is where the June incident lives |

## Sprint 2 — Catalogue, policy, candidate lifecycle (Phase 2)

| Task | Work in this repo | Notes |
|---|---|---|
| HF-008 Catalogue model & loader | `windmill/capability-index.yaml` + loader/validator script under `f/hermes_flow/catalogue/`; CI check that every catalogue path exists | CI = existing `make lint validate ci` chain |
| HF-009 Search & ranking | Agent-facing search op (via HF-000B transport) with primitives-before-workflows ranking; fixed evaluation set as test fixture | Deterministic, no LLM in ranking |
| HF-010 Policy evaluator | Deterministic evaluator under `f/hermes_flow/policies/`; fails closed on missing metadata; table-driven tests | Must not require an LLM |
| HF-011 Candidate creation | Admin op writing to the candidate namespace; idempotent by request key; records reason + base version | Never touches active paths |
| HF-012 Diff & impact analysis | Diff candidate vs active; consumer traversal from catalogue dependency declarations; cycle-safe | |
| HF-013 Promotion workflow | Windmill flow: policy check → required tests → evidence → (approval) → write active + provenance; stale-base conflict detection | Use Windmill's native approval steps |
| HF-014 Deprecation & rollback | Deferred to hardening sprint per §15 | |

## Sprint 3 — Testing, lineage, artifacts, first capability (Phase 3 + start of 4)

| Task | Work in this repo | Notes |
|---|---|---|
| HF-015 Test conventions & runner | `windmill/tests/{fixtures,contracts,workflow_cases}/`; runner distinguishes promotion-gating vs scheduled tests | |
| HF-017 Artifact adapter | Content-addressed store over the HF-000A mount; SHA-256; traversal-safe paths | `f/libraries/storage/` |
| HF-018 Lineage helpers | Context propagation + derivation links through flow steps | `f/libraries/lineage/` |
| HF-019 Hermes structured-invocation wrapper | Extend `f/hermes/client.py` pattern: new `f/libraries/ai/invoke_hermes_structured` capability — schema-validated output, prompt/conversation/raw-response artifacts, model metadata, retries; marked nondeterministic | Reuses `hermes_endpoint` resource; verify secrets never land in retained artifacts |
| HF-021 Web fetch capability | `f/capabilities/collection/` or `f/libraries/web/`; allowed-domain policy, size/timeout bounds, raw artifact retention | SSRF tests: disallowed domains, local-network addresses — matters because Windmill workers sit on the internal Docker networks |
| HF-016 Dependency-aware regression selection | Test selection from the catalogue dependency graph; cycle-safe traversal with per-test inclusion rationale | Pulled forward from hardening so HF-020 can run in this sprint |
| HF-020 Scheduled health tests | Scheduled flows generated from capability metadata; bounded samples, rate limits, consecutive-failure counts | Pulled forward from hardening — catches source drift during exemplar development |

## Sprint 4 — Product-collection exemplar (Phase 4)

| Task | Work in this repo | Notes |
|---|---|---|
| HF-022 Structured markup extraction | JSON-LD first; deterministic on fixtures | |
| HF-023 Product extraction | Deterministic → known parser → HF-019 AI fallback, with provenance | |
| HF-024 Product normalisation | Deterministic, versioned schema; table-driven price/currency tests | |
| HF-025 Snapshot persistence | Versioned migration in `collection_db/`; idempotent upserts keyed by execution/source/product; rows carry trace + artifact refs | Integration-test against a disposable Postgres, not the live `collection_db` |
| HF-026 Comparison & report | Golden-file tests; report stored as artifact | |
| HF-027 Assemble workflow | `f/workflows/product_collection/` flow; bounded concurrency; per-source failure isolation | End-to-end smoke with three fixture-backed sources |
| HF-028 Conversational creation & execution | Teach the HF-005 skill to satisfy a natural-language request end to end | Prompt evaluation set incl. ambiguous and unsupported-write requests |

## Sprint 5 — Adaptive repair (Phase 5)

HF-029 failure-inspection package → HF-030 repair-candidate generation →
HF-031 drift fixture promotion → HF-032 orchestrated repair/retry with attempt
limits. All as specified in the document; no repo deltas beyond building on
Sprint 2/3 pieces. The demo scenario is a deliberately changed retail fixture.

## Sprint 6+ — Hardening (Phase 6 + deferred Phase 2 items)

HF-014 rollback, HF-033 health dashboard (Grafana — observability stack
already optional in compose), HF-034 rollback recommendation, HF-035
retention/privacy/cost, HF-036 documentation and clean-install walkthrough.

## Standing constraints (from the document's definition of done + this repo's history)

- All `wmill` CLI usage goes through the Makefile targets (`make windmill-push`
  / `windmill-pull` / `windmill-check`) — never bare `wmill` commands; sync
  scope changes are reviewed against `docs/windmill-sync.md` and rehearsed on a
  disposable workspace first.
- New Windmill assets are Python, carry explicit metadata, and enter
  `wmill.yaml` `includes:` one path at a time.
- No secrets in prompts, fixtures, logs or artifacts; `skipSecrets: true`
  stays on.
- Every executed task returns a Windmill job reference; no silent
  direct-execution fallback in Hermes.
