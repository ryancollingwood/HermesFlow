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
| "Establish safe Windmill namespace and sync plan" (HF-007) | `windmill/wmill.yaml` is already narrowly scoped post-incident (2026-06-29); `make windmill-push/pull/check` and `docs/windmill-sync.md` exist | HF-007 (done): defined the four namespaces, added `f/hermes_flow/` to `includes:` item-by-item, live-verified the candidate namespace stays out of sync |
| AI wrapper built from scratch (HF-019) | `f/hermes/client.py` already provides an OpenAI-compatible Hermes client + `hermes_endpoint` resource type | HF-019 extends the existing client with schema validation, artifact retention and metadata capture |
| Skill deployment "commands include the new skill" (HF-005) | `make hermes-skills-push` is additive over `hermes/skills/<category>/<skill>/` | HF-005 only needs the skill authored in the right place; deployment is free |
| Language unstated (JSON Schema *or* Pydantic) | All Windmill assets are Python; `defaultTs: bun` in wmill.yaml is only the CLI default | Use **Pydantic** models as source of truth; export JSON Schema for docs/CI |
| "existing collection database" (HF-025) | `collection_db/` Postgres + `f/collection/collection_db.resource.yaml` exist | HF-025 targets that database; migrations live under `collection_db/` |
| Hermes ↔ lifecycle controls "via MCP" (§4 diagram) | **Already wired, undocumented**: `hermes mcp list` shows a live, enabled `windmill` MCP connection (native Streamable-HTTP, no bridge), token created 2026-06-21 — no ADR, no Makefile target, no mention anywhere in the repo | New task **HF-000B** below — document the existing wiring, prove it live, and flag it for productization |
| "content-addressed mounted artifact filesystem" (HF-017) | No new volume needed — `${SHARED_DIR}` is already mounted into Hermes and all Windmill services; just needed an `artifacts/` subdir provisioned + documented (task **HF-000A**, now done) | HF-017 builds its storage adapter directly on `${SHARED_DIR}/artifacts/`, no compose change required |

## Added tasks (gaps in the document)

### HF-000A — Provision the artifact store volume (done)
**Phase 1.** HF-017 assumes a mounted filesystem exists. Initial framing
called for a new named volume or `data/artifacts/` bind mount. Investigation
found that's unnecessary: `${SHARED_DIR}` is *already* bind-mounted into
`hermes`, `windmill_server`, `windmill_worker`, and `windmill_worker_native`
(`docker-compose.yml`) — the same mount the data-platform pipeline already
reuses for `${SHARED_DIR}/datalake/` (`docs/plans/datalake.md`). So this
became provisioning a subdirectory + updating docs, not a compose change:
`make init`/`install.py` now create `${SHARED_DIR}/artifacts/`, and the
existing recursive `fix-permissions`/`install.py` chown step covers it with
no separate permission logic. Recorded in
`architecture/adr/0003-artifact-lineage-model.md`.
*Exit (met):* a Windmill job (`POST .../jobs/run/preview`) wrote and re-read
a file under `/shared/artifacts/`, and it survived `docker restart` of the
worker container.

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

**Follow-up (done):** the MCP wiring is now reproducible via `make
windmill-mcp` — mints/reuses a token scoped to `mcp:all` (required just to
reach the MCP endpoint — Windmill gates it behind a separate `mcp:*` scope
family, independent of the REST scopes below; narrower `mcp:` values connect
but expose zero tools) plus `scripts:read`/`flows:read`/`jobs:read`/
`jobs:run:scripts`/`jobs:run:flows` (no `*:write` — verified writes still
403 at call time even though `mcp:all` makes write-shaped tools visible) via
Windmill's own token-scoping API, registers with Hermes, verifies, and
leaves an already-working connection untouched (idempotent, non-destructive).
See the ADR's Consequences section and
`docs/windmill-sync.md#windmill-mcp-registration`.

## Sprint 1 — Execution contract and foundations (Phase 1)

| Task | Work in this repo | Notes |
|---|---|---|
| HF-001 Exclusive-execution ADR | Create `architecture/adr/0001-windmill-exclusive-execution.md`; link from README | New top-level dir; also seed ADRs 0002–0004 as stubs per §11 |
| HF-000B Transport ADR | `architecture/adr/0005-…`; prove one round trip | Blocks HF-005/006/008+ |
| HF-000A Artifact volume (done) | `artifacts/` subdir on the existing `${SHARED_DIR}` mount + docs | Blocks HF-017 |
| HF-002 Context & artifact schemas (done) | `ExecutionContext`/`ArtifactRef` Pydantic models in `f/libraries/lineage/models.py`; `schema_version` field; 21 unit tests in `windmill/tests/`; JSON Schema exported to `docs/schemas/` and drift-checked by a test | Importable via `from f.libraries.lineage.models import …` (same pattern as `f.hermes.client`); see `architecture/adr/0003-artifact-lineage-model.md` |
| HF-004 Result envelope (done) | `ExecutionResult` in `f/libraries/results/models.py`; `render_summary()` + `docs/result-envelope-rendering.md` guidance (destined for the HF-005 skill); 17 snapshot/validation tests | `success`/`partial` cannot validate without a Windmill job ref (schema-enforced, not convention); see `architecture/adr/0001`'s Consequences |
| HF-003 Capability metadata & autonomy schema (done) | `CapabilityMetadata`/`AutonomyPolicy` Pydantic models in `f/libraries/capability/models.py`; promote/schedule structurally pinned to `approval_required`; 24 unit tests incl. two worked examples (read-only web capability, write capability) and a maturity×effects sweep proving "low-risk label ≠ promote permission" | Feeds `windmill/capability-index.yaml` (HF-008); see `architecture/adr/0002-capability-lifecycle.md` |
| HF-005 Orchestration skill skeleton (done) | `hermes/skills/workflow-orchestration/hermesflow/SKILL.md` + `capability-selection.md`, `generation-policy.md`, `repair-policy.md`, `result-presentation.md` | Follows existing `<category>/<skill>/SKILL.md` + references convention; deployed by existing `make hermes-skills-push` (verified: `hermes skills list` shows it `enabled` after push). Three live `hermes chat -s hermesflow` prompt tests: **reuse-before-generate** — correctly walked primitives → workflow → generation order, cited `CapabilityMetadata` fields from `capability-selection.md`'s worked example; **Windmill-unavailable** — correctly refused, produced a well-formed `failure`-outcome `ExecutionResult` narrative citing ADR 0001, `job` correctly absent; **direct-execution prohibition** — asked to fetch a URL "directly, don't overthink it," the model called the built-in `web_extract` tool anyway despite Rule 1 — see HF-006 |
| HF-006 Audit/restrict Hermes direct execution (done) | `docs/hermesflow-session-scoping.md`: inventory of 7 execution-capable built-in toolsets (`terminal`, `code_execution`, `browser`, `file`, `web`, `computer_use`, `cronjob`); mechanism is `hermes chat`'s session-scoped `-t` allowlist (`windmill,memory,todo,clarify,session_search`), not global `hermes tools disable`; `hermesflow` SKILL.md Rule 1 updated to mandate this invocation | Live-tested all four vectors (shell/browser/filesystem/Python) against a scoped session — each reported the tool as absent, not declined; ordinary conversation confirmed unaffected. `delegation` toolset deliberately left out and flagged as an open sub-agent-scoping gap, not silently resolved |
| HF-007 Namespace & sync plan (done) | Four namespaces documented (`docs/windmill-sync.md`, `AGENTS.md`): `f/libraries/` (already in `includes` wholesale since HF-002 — retroactively added to `windmill-sync.md`'s scope table, which had been missed) and `f/hermes_flow/` (new; `folder.meta.yaml` only so far, enumerated item-by-item per the `f/data_platform` pattern) are in scope now; `f/capabilities/` and `f/workflows/` documented as reserved, added the day either gains real content. `f/hermes_flow/candidates/` (HF-011) is excluded both by omission from `includes` and defensively by an explicit `excludes` entry | Live-tested the candidate exclusion against the actual `main` workspace (not a separate disposable one — this workspace already carries real unrelated-asset drift-detection stakes from earlier sessions): created a script directly on the server at `f/hermes_flow/candidates/hf007_probe`, ran `make windmill-check` — only drift reported was `folder.meta.yaml`'s server-stamped `owners` field, the candidate never appeared; confirmed via a real `wmill sync pull` that it was never written to `windmill/f/hermes_flow/`. Cleaned up (archived) the probe script afterward |

## Sprint 2 — Catalogue, policy, candidate lifecycle (Phase 2)

| Task | Work in this repo | Notes |
|---|---|---|
| HF-008 Catalogue model & loader (done) | `Catalogue`/`CatalogueEntry` in `f/hermes_flow/catalogue/models.py` (wraps `CapabilityMetadata` + `kind`/`tags`/`inputs_summary`/`outputs_summary`); `windmill/capability-index.yaml` with 2 real entries (`f/hermes/client`, `f/data_platform/extract_hn_stories`); 18 tests incl. empty/valid/duplicate/malformed catalogues and a path-existence check | `load_catalogue()` names the offending entry (by path, or index if unparseable) and field on every validation error; CI (`make test`) fails if a catalogue path has no matching `.py` file — see `windmill/tests/test_catalogue.py` |
| HF-009 Search & ranking (done) | `search()`/`SearchQuery`/`SearchResult` in `f/hermes_flow/catalogue/search.py`, exposed via HF-000B's MCP transport (`runScriptByPath`); deterministic keyword/tag/kind scoring with a primitive bonus and an effects penalty; 22 tests incl. a fixed evaluation set, exact-tag/keyword/schema-compatibility search, and a controlled pair proving side-effect capabilities aren't silently preferred | No LLM/embeddings — plain token overlap with a minimal stopword filter; `CatalogueEntry` gained `input_kinds`/`output_kinds` (additive, HF-008's `schema_version` unchanged) for real (if convention-based, not type-checked) schema-compatibility matching |
| HF-010 Policy evaluator (done) | `evaluate_policy()`/`PolicyContext`/`PolicyDecision` in `f/hermes_flow/policies/evaluator.py`; fails closed (denied, not approval_required) on unknown capabilities; promote/schedule always approval_required; destructive-flagged and limit-exceeding requests escalate/deny even when the capability's own `AutonomyPolicy` says automatic; 45 tests incl. a full action×context table | Deterministic — no LLM; a decision can only be as permissive as the capability's declared `AutonomyPolicy`, never more so |
| HF-011 Candidate creation (done) | `create_candidate()` in `f/hermes_flow/candidate_ops/create.py`; deterministic `compute_candidate_id(request_key)` makes creation idempotent without a separate ledger — the candidate's own existence at its deterministic path *is* the check; `CandidateRecord` (`candidate_ops/models.py`) records reason, provenance (`source_path`/`base_version`, auto-resolved from the source's live Windmill hash when deriving), conversation/request references; 20 unit tests against an in-memory fake client | **Privileged-gateway pattern discovered live**: Windmill jobs run with the *script owner's* permissions (`WM_PERMISSIONED_AS`), not the caller's token scope — confirmed empirically (a job triggered by a token scoped to only `scripts:read`/`jobs:run:scripts` still created a script successfully). So Hermes's narrowly-scoped `windmill-mcp` token never needs `scripts:write`; it only needs to call *this one script*, whose own elevated identity does the write. Safety is enforced by this module's own path-escape check, not by token scopes. Live-verified all four testing-guidance points (new candidate, idempotent duplicate, derived candidate with auto-resolved `base_version`, active asset byte-for-byte unchanged) against the real server using a deliberately narrow-scoped token, not admin |
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
