# Evaluating Revornix against HermesFlow

**Subject:** [Qingyon-AI/Revornix](https://github.com/Qingyon-AI/Revornix) —
"open-source, local-first AI information workspace."
**Evaluated at:** `master` @ `08cf510` (2026-07-06); latest published Docker
images `v0.7.0` (2026-03-07).
**Question:** what does it do that we already have, what does it do that we
don't, and how could it be integrated?

## Verdict in one paragraph

Revornix and HermesFlow solve adjacent problems that look similar from a
distance and are almost disjoint up close. Revornix is a **knowledge
workspace**: it optimises the path *ingest → convert → index → summarise →
publish → notify*, and its unit of value is a **document** in a personal
library. HermesFlow is a **governed execution platform**: it optimises the
path *request → discover-or-generate a capability → gate → execute →
monitor → repair*, and its unit of value is a **versioned, tested
capability** with a real job reference. The genuine overlap is thin — both
are self-hosted Docker stacks, both talk to OpenAI-compatible providers,
both speak MCP, both notify over chat channels. The complement is thick:
Revornix has an entire document/RAG/media/publishing surface we have
nothing like, and HermesFlow has an entire lifecycle-governance surface
Revornix has nothing like. Integration is therefore attractive **in one
direction only** — Revornix as an external *store* that HermesFlow reads
from and writes to, never as a second execution engine.

---

## 1. What Revornix actually is

Six services plus five datastores:

| Component | Role |
|---|---|
| `web/` | Next.js workspace UI + SEO-optimised public pages |
| `gateway/` | Go public entry point — routing, anti-scraping, upstream failover |
| `api/` | FastAPI core — auth, documents, sections, AI, admin, MCP servers |
| `celery-worker/` | Async workflows — convert, chunk, embed, graph, summarise, tag, transcribe, podcast, PPT, notify |
| `hot-news/` | Trending aggregation (DailyHotApi fork) |
| `docs/` | Separate Next.js + Nextra docs site |
| Postgres / Redis / Neo4j / Milvus (+ etcd + MinIO) / MinIO | Relational, cache+broker, knowledge graph, vector index, object storage |

The pluggable-engine surface is real, not aspirational —
`api/engine/` ships concrete implementations per category:
`markdown/` (MinerU, Jina, markitdown), `embedding/` (Qwen cloud/local),
`tts/` (OpenAI audio, Volcengine), `stt/` (Volcengine fast/standard),
`image_generate/` (Bailian, Banana, OpenAI, Volc), `image_understand/`
(Kimi), `tag/` (LLM), `video_plugins/` (Bilibili, YouTube). Notifications
are similarly plugin-shaped: `notification/{source,target,tool,template,
trigger_event}/` with email, Telegram, Feishu, DingTalk and Apple push
implementations behind a `protocol/` interface.

Its MCP server surface (`api/mcp_router/`) is substantial: ~20 document
tools (search / vector-search / unread / recent / starred / detail /
labels / notes / star / read-status / **create / update / delete**), three
graph tools (`search_mine_graph`, `search_document_graph`,
`search_section_graph`), plus section and common routers. Auth is a plain
`api-key` request header validated against a per-user API key
(`api/mcp_router/auth.py`), served over FastMCP's `http_app()` — i.e.
Streamable HTTP, which Hermes's `--url` MCP client can drive **directly**,
without the `mcp-remote` shim Baserow needs.

---

## 2. What it does that we already have

| Revornix capability | HermesFlow equivalent | Assessment |
|---|---|---|
| Self-hosted Docker stack behind a public gateway | Caddy ingress, six segmented networks, per-service resource limits + logging (`AGENTS.md`) | **Ours is more operationally mature.** We have dual idempotent installers, `make secrets`, backups, health probes, and an observability override. Revornix's `docker-compose-local.yaml` only starts *dependencies*; the services themselves are documented as run-by-hand in per-service conda envs. |
| Model-flexible, any OpenAI-compatible provider | Hermes gateway + `--provider openrouter\|anthropic\|openai`, Ollama override, MLX on Apple Silicon, Headroom compression proxy | **Ours is broader** — Revornix has no local-inference or context-compression story. |
| Knowledge graph + multi-strategy retrieval | Hindsight (fact extraction, entity resolution, KG, semantic/keyword/temporal/graph retrieval on pgvector) | **Conceptually overlapping, functionally different.** Hindsight remembers *the agent's conversations and facts*; Revornix indexes *a document corpus*. Neither substitutes for the other. |
| MCP client **and** MCP server | Hermes is an MCP client; Windmill, Baserow and Directus are registered MCP servers | **Same pattern, already established.** Revornix would be a fourth registration, and an easier one than Baserow (no SSE shim). |
| Chat-channel notifications | Hermes Telegram + Discord channels; `f/hermes/telegram.py` document delivery from Windmill jobs | **Ours is narrower** (two channels vs. six) but present and secret-bootstrapped. |
| Structured records with a UI *and* a REST API | Baserow (`--with-baserow`), Directus (`--with-directus`) | Already solved, twice, both MCP-exposed. |
| Scheduled / automated pipelines | Windmill schedules; `data-platform/` dlt → dbt → Postgres | Already solved, and ours carries lineage and approval gating Revornix's Celery schedules don't. |
| Markdown report generation | `render_product_report.py`, `docs/result-envelope-rendering.md` | Ours is golden-tested and artifact-retaining; Revornix's is richer in presentation (illustrations, Tiptap editor). |
| Document parsing / OCR | `ocr-with-liteparse` skill (liteparse + tesseract baked into the Hermes image) | **Ours is much narrower** — one CLI, no per-type engine selection. |
| Web page → structured content | `web_fetch.py` (allowlisted domains, public-address-only DNS/peer checks, bounded streaming, raw artifact retention) + `extract_structured_markup.py` | **Ours is far more hardened but single-purpose.** Revornix ingests *anything you throw at it*; we fetch product pages safely. Different goals. |
| API keys / auth | Windmill scoped tokens, `API_SERVER_KEY` | Revornix adds TOTP, passkeys, and Google/GitHub/WeChat/phone OAuth — necessary for a multi-user product, surplus for a single-operator stack. |

**Net:** everything in this table is either already covered, or covered
better, by the existing stack — with the single exception of document
parsing, where Revornix's engine roster is genuinely ahead of our one OCR
skill.

---

## 3. What it does that we don't have

1. **A document library as a first-class object.** Persistent per-user
   corpus with unread/starred state, labels, notes, comments, and both
   text and vector search over it. We have content-addressed *artifacts*
   with lineage and retention classes — excellent provenance, no library
   semantics and no query surface for a human.
2. **Chunked vector retrieval over a document corpus (Milvus) plus
   per-user GraphRAG (Neo4j).** Hindsight is not this. This is the single
   most substantive gap.
3. **A converter engine roster** — MinerU / Jina / markitdown for
   PDF/Word/Excel/PPT/HTML, selectable per workspace or per document type.
4. **Audio ingestion** — STT with a meeting mode for speaker separation.
5. **Generated media** — two-voice podcast synthesis (regenerable when the
   source changes), AI illustrations embedded into long-form content, PPT
   generation from a section.
6. **Sections and day-sections** — curated collections, and automatic
   daily digests of what you saved.
7. **A publishing/social surface** — public documents, creator and label
   pages with SEO, community feed, subscriptions, collaboration requests.
8. **A trending aggregator** (`hot-news/`).
9. **A rich reading/editing UI** — Tiptap with tables, Mermaid, math, TOC.
10. **A broader notification fan-out abstraction** — sources, targets,
    tools, templates and trigger events as pluggable protocols, with six
    channels implemented.
11. **A Python SDK / CLI** for programmatic ingestion.
12. **Multi-user product plumbing** — MFA, OAuth, rate limiting,
    edge-level anti-scraping, i18n.

Of these, **1–5 are worth wanting**. 6–12 are the trappings of a
multi-tenant SaaS product and are cost, not benefit, for a single-operator
stack.

---

## 4. What we have that Revornix doesn't

This matters because it defines what any integration must not break.

- **A governed capability lifecycle.** Candidate namespace, candidate↔active
  diff, reverse-dependency impact analysis, mandatory native Windmill
  approval suspension before promotion, versioned promotion records
  (ADR 0002, HF-011–HF-014).
- **A deterministic, fail-closed policy evaluator** — unknown capability
  means denied, not "ask"; `promote`/`schedule` are structurally pinned to
  `approval_required` regardless of how low-risk the capability looks.
- **Lineage and artifact provenance** — content-addressed storage,
  `derived_from` chains, retention classes, cost/size/record limits, and
  tombstones that keep a deleted artifact's lineage resolvable.
- **Dependency-aware regression selection, scheduled health tests, a
  capability health dashboard,** and Prometheus/Grafana projection.
- **Adaptive repair** — failure classification, schema-validated repair
  candidate generation, source-drift regression fixtures, one bounded
  approval-gated retry — and **automatic rollback recommendation**.
- **The Windmill-exclusive execution boundary** (ADR 0001) with real
  enforcement via session toolset scoping.
- **Operational maturity** — two idempotent installers, secret generation
  and rotation guidance, backups, resource limits and logging on every
  service, CI-validated compose.

Revornix has effectively none of this. Its Celery workflows are
hand-written modules; there is no capability catalogue, no approval gate,
no lineage, no repair loop. **This asymmetry is the whole reason
integration has a preferred direction.**

---

## 5. Frictions to price in before integrating

**a. It ships a second execution engine.** `celery-worker/` runs task code:
network fetches, conversions, LLM calls, TTS, file writes, notifications.
ADR 0001 says *all executable task code runs through Windmill*. There is a
defensible framing — Hindsight already performs LLM fact-extraction
internally and we classify it as an external store, and ADR 0001's own
responsibilities table has a row for "External stores … durable
raw/intermediate/final artifacts" — but Revornix is a much larger instance
of it. **This needs a written ADR, not an assumption.** The line to hold:
Revornix processing *its own* documents on *its own* schedule is store
behaviour; Revornix executing something HermesFlow asked for, as a task,
is a boundary violation.

**b. Footprint.** A full deployment adds roughly ten containers — web,
gateway, api, celery-worker, hot-news, plus Postgres, Redis, Neo4j, MinIO,
and Milvus (which itself needs etcd + MinIO + standalone). Neo4j and
Milvus alone are multi-gigabyte resident. `AGENTS.md` requires resource
limits and logging on every service, so this is real authoring work on top
of the RAM.

**c. Packaging gap.** On `master` there are Dockerfiles for `gateway/` and
`hot-news/` only. The `docker-push.yml` workflow builds `web`, `hot-news`,
`celery-worker` and `api` from `<service>/Dockerfile` — files not present
in `master`. Published `revornix/*` images exist but the newest is
`v0.7.0` from 2026-03-07, four months behind `master`. So an override
either **pins stale images** or **carries our own Dockerfiles** for three
services. Neither is free.

**d. Single-operator vs. multi-tenant.** Revornix assumes accounts,
sessions, public pages and an anti-scraping edge. Exposed through Caddy,
that is the largest new attack surface the stack would have taken on.

**e. Licence.** Apache-2.0 with Dify-style additional conditions:
self-hosting is fine; **operating a multi-tenant environment requires a
commercial licence**; the `web/` frontend's logo and copyright may not be
removed. Lifting code into `windmill/f/` is Apache-2.0-governed and needs
attribution — and HermesFlow currently has **no `LICENSE` file at all**,
which should be fixed before any code is copied in.

---

## 6. Integration options

### Option 0 — Reference only

Read it, take the ideas, run nothing. Zero cost, zero footprint, and we
keep the converter-engine and notification-plugin patterns as design
inputs.

### Option 1 — Peer service + read-only MCP (the Baserow pattern) — **recommended entry point**

`docker-compose.revornix.yml` as an opt-in override, `make revornix` to
layer it in, `make revornix-mcp` to register its MCP endpoints with
Hermes. Exactly the shape `--with-baserow` / `--with-directus` already
established, and technically easier: FastMCP's Streamable HTTP works with
`hermes mcp add --url` directly, and auth is a single `api-key` header —
no `mcp-remote` stdio shim.

The critical constraint: Revornix's document MCP router exposes
`create_document`, `update_document`, `delete_documents`,
`create_document_label`, `create_document_note`, `set_document_*`. Those
are state mutations on the user's behalf — task work under ADR 0001.
**Register the read surface for Hermes; route every write through a
catalogued Windmill capability** calling Revornix's REST API. That keeps
writes inside the policy evaluator, gives them lineage and a job
reference, and preserves the "no silently faked result" property.

Gain: the agent can search, cite and reason over a real document corpus
and its knowledge graph. Cost: the footprint and packaging problems in §5.

### Option 2 — Revornix as the library substrate behind HermesFlow capabilities

Builds on Option 1. Add `f/capabilities/library/*` — `library_ingest`,
`library_search`, `library_summarise` — each catalogued in
`capability-index.yaml`, policy-gated, lineage-linked, artifact-retaining,
and each returning a proper `ExecutionResult` with a Windmill job ref.
Revornix's Celery does the conversion/embedding/graph work internally, as
store behaviour; HermesFlow never runs task code there.

This is the highest-value deep integration and the one that fits the
architecture: it closes gaps 1–5 without importing a second lifecycle.

### Option 3 — Harvest components, run nothing

Port the good ideas into Windmill capabilities instead: a
`convert_document` capability with a selectable MinerU/Jina/markitdown
engine, a TTS capability, extra notification targets alongside the
existing Telegram delivery. Everything stays inside the governed
lifecycle; nothing new goes in the compose file.

Gain: architectural purity, no footprint, no stale-image problem. Cost:
this is real build work, we get no UI, and we'd be reimplementing rather
than reusing — while inheriting Apache-2.0 attribution obligations for
anything actually copied.

### Option 4 — Replace Hindsight with Revornix's Milvus + Neo4j

**Reject.** Different jobs — agent memory vs. document corpus — and
Hindsight is wired into Hermes as a first-class memory plugin
(`hindsight_retain` / `_recall` / `_reflect`). Swapping it would cost
working functionality to gain nothing.

### Option 5 — Reverse direction: expose HermesFlow to Revornix

Revornix is also an MCP *client*, so it could call Windmill capabilities.
Marginal: Revornix's UI is not where we want capability execution
initiated, and it would route execution requests around Hermes's session
scoping.

---

## 7. Recommendation

Staged, with a cheap kill-switch at the front.

**Stage A — spike, out of stack (hours, no repo changes).** Run Revornix
standalone from published `v0.7.0` images on a scratch compose file
outside this repo. Ingest a realistic corpus. Register its MCP read tools
with a throwaway Hermes session and judge one thing only: *does grounding
on a document library plus GraphRAG measurably improve answers over what
Hindsight already gives us?* If it doesn't, stop at Option 0 — the ideas
were the valuable part.

**Stage B — if Stage A pays off (Option 1).** Write ADR 0006 classifying
Revornix as an external store rather than an execution engine, with the
store/task line drawn explicitly. Add `docker-compose.revornix.yml` +
`make revornix` / `make revornix-mcp` following the Baserow precedent, with
resource limits and logging per `AGENTS.md`. Register the **read** MCP
surface only. Decide the image question deliberately — pin `v0.7.0` and
accept the lag, or carry three Dockerfiles and track `master`. Add a
`LICENSE` file to HermesFlow first.

**Stage C — optional (Option 2).** Add the `f/capabilities/library/*`
capabilities so ingestion and retrieval become first-class, catalogued,
policy-gated capabilities with lineage — and so Revornix writes never
happen outside a Windmill job.

**Explicitly out of scope at every stage:** the publishing/community
surface, SEO pages, trending feed, and multi-user auth. They are cost and
attack surface for a single-operator stack, and the licence's multi-tenant
clause makes the sharing features the least useful part of the package
anyway.

**And do not** let `celery-worker/` become a place where HermesFlow tasks
run, and do not retire `web_fetch.py` in favour of Revornix ingestion —
its SSRF hardening, domain allowlisting and raw-artifact retention are
load-bearing for the product-collection exemplar and have no equivalent on
the Revornix side.
