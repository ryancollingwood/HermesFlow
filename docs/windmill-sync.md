# Windmill sync — scope, scenarios, and conventions

This explains exactly what `wmill sync` does to your content, so a push never
surprises you. The golden rule:

> **`push` and `pull` are one-way *mirrors*, not merges.** Each makes its target
> match its source — which means each can *delete* content on its target side.
> `push` puts **server** content at risk; `pull` puts **repo** content at risk
> (but git can restore it).

> [!WARNING]
> **Never run `wmill` commands directly from anywhere other than this
> `windmill/` directory.** All scope protection in this doc comes from
> `wmill.yaml` — if the CLI can't find it (wrong cwd), it prints `No
> wmill.yaml found. Use 'wmill init' to bootstrap it.` and falls back to
> **zero scope restriction**, diffing the entire remote workspace against
> whatever's in your *current* directory. Run from the repo root, this
> deletes/archives everything on the server that isn't under a literal
> `f/` folder at your cwd — which, from the repo root, is nothing. This is
> not hypothetical: running `wmill sync push --yes --skip-branch-validation`
> from the repo root once hard-deleted every secret variable, resource, and
> folder in the workspace and archived every script, including several
> never tracked in this repo at all (`f/karakeep/*`, `f/lmstudio`). Folders/
> resources/variables in scope were recoverable via `make windmill-push`;
> the rest needed manual UI unarchiving, and three secret values were
> permanently lost (no Postgres backup existed at the time — see `make
> backup`/`make backup-schedule`). **Always prefer `make windmill-push` /
> `make windmill-pull` / `make windmill-check`** — they `cd windmill` for
> you and (for push) dry-run first and abort on any deletion. If you must
> run `wmill` by hand, `cd windmill` first and confirm you see `Using
> workspace 'main' …` (not the `No wmill.yaml found` warning) before any
> non-dry-run command.

## Folder layout & sync scope

Sync scope is set by `includes`/`excludes` in [`wmill.yaml`](wmill.yaml) — **not**
by folder boundaries. It is deliberately narrow:

| Path | Purpose | Versioned in repo? | In sync scope? |
|---|---|---|---|
| `f/hermes/**` | Hermes↔Windmill **code/config** | ✅ yes | ✅ yes |
| `f/collection/**` | collection_db Postgres resource + Baserow webhook receiver | ✅ yes | ✅ yes |
| `f/libraries/**` | shared, importable Pydantic-model modules (`f.libraries.lineage.models`, `f.libraries.capability.models`, `f.libraries.results.models`, …) | ✅ yes | ✅ yes |
| `f/capabilities/**` | versioned active capabilities, beginning with HF-021's policy-bounded web fetch | ✅ yes | ✅ yes |
| `f/data_platform/{folder.meta,data_platform_db.resource,db_password.variable,dbt_run.*,extract_hn_stories.*}` | dlt/dbt pipeline scripts + their resource/secret | ✅ yes | ✅ yes (named explicitly, not wildcarded) |
| `f/hermes_flow/{folder.meta.yaml,catalogue/models.*,catalogue/search.*,policies/evaluator.*,candidate_ops/models.*,candidate_ops/create.*,candidate_ops/diff.*,candidate_ops/promote.*,candidate_ops/prepare_promotion.*,candidate_ops/promotion.flow/**,candidate_ops/lifecycle.*,testing/runner.*,testing/example_test.*,testing/regression.*,testing/scheduled_health.*,testing/health_report.*,testing/source_drift_fixture.*,repair/folder.meta.yaml,repair/models.*,repair/inspection.*,repair/generate_candidate.*,repair/promote_fixture.*}` | HermesFlow's own control-plane: capability catalogue/search/policy (HF-008–010), candidate lifecycle (HF-011–014), testing/regression/health reporting (HF-015–016, HF-020, HF-033), and bounded adaptive repair (HF-029–032) | ✅ yes | ✅ yes (named explicitly, not wildcarded — see below) |
| `f/workflows/{folder.meta.yaml,product_collection.flow/**}` | composed, versioned workflows beginning with HF-027's bounded product collection flow | ✅ yes | ✅ yes (named explicitly, not wildcarded) |
| `capability-index.yaml` (top level, not under `f/`) | the version-controlled capability index itself, validated/loaded by `f/hermes_flow/catalogue/models.py` | ✅ yes | ❌ no — repo-only, like `wmill.yaml` itself; not a Windmill script/flow/resource asset, so there's nothing for `wmill sync` to push. Read directly from the checked-out repo by CI and by whatever eventually calls `load_catalogue()` |
| `hermes_endpoint` resource-type | the shared endpoint type | ✅ yes | ✅ yes |
| `f/hermes_state/**` | **runtime state** (timestamps, cursors, non-secret vars) | ❌ no | ❌ **no** |
| `f/hermes_flow_state/**` | **lifecycle runtime state** (deprecation, rollback, and scheduled-health records) | ❌ no | ❌ **no** |
| **`f/hermes_flow/candidates/**`** | **candidate capabilities** (HF-011) — proposed, not-yet-promoted code | ❌ no | ❌ **no**, explicitly excluded (see below) |
| any other item dropped into `f/data_platform/`, `f/hermes_flow/`, or `f/workflows/` not named above | unenumerated pipeline/catalogue/workflow assets | n/a | ❌ **no** |
| any other `f/*` folder | unrelated projects | ❌ no | ❌ no |
| all `u/*` (incl. a future `u/hermes`) | user namespaces | ❌ no | ❌ no |
| inherited resource-types (the ~250 from `admins`, incl. the built-in `postgresql` type used by `f/collection/collection_db`) | Windmill built-ins | ❌ no | ❌ no |
| secret variables (anywhere) | credentials | ❌ no (placeholder only) | ❌ no (`skipSecrets`) |

Two settings enforce this:

- **`includes`** — see [`wmill.yaml`](wmill.yaml) for the live list. `f/hermes/**`,
  `f/collection/**`, `f/libraries/**`, and `f/capabilities/**` are wholesale wildcards; the glob is
  separator-aware, so none of them matches `f/hermes_state`, each other's
  folders, or any unrelated `f/*`. These three hold only versioned code with
  no runtime-state or candidate subfolder living alongside it, so a wildcard
  is safe.

  `f/data_platform/`, `f/hermes_flow/`, and `f/workflows/` are different: their entries name
  each file/pattern explicitly instead of a `**` wildcard, for two distinct
  reasons —
  - `f/data_platform/` (`f/data_platform/dbt_run.*`, `f/data_platform/extract_hn_stories.*`,
    plus the folder's `folder.meta.yaml`, resource, and secret variable) is
    the folder most likely to grow extra scripts/flows/apps over time (on
    the server or locally), and a wildcard there would silently widen the
    mirror's blast radius every time someone adds something without
    updating this doc. Adding a new pipeline means adding its files to
    `includes` explicitly; see
    [`docs/data-platform-add-pipeline.md`](data-platform-add-pipeline.md).
  - `f/hermes_flow/` (currently `f/hermes_flow/folder.meta.yaml`,
    `f/hermes_flow/catalogue/models.*`, `f/hermes_flow/catalogue/search.*`,
    `f/hermes_flow/policies/evaluator.*`, and
    `f/hermes_flow/candidate_ops/{models,create,diff,promote,prepare_promotion,lifecycle}.*`,
    `f/hermes_flow/testing/{runner,example_test,regression,scheduled_health,health_report,source_drift_fixture}.*`, plus
    `f/hermes_flow/repair/{folder.meta.yaml,models.*,inspection.*,generate_candidate.*,promote_fixture.*,orchestrate.*,finalize_retry.*,adaptive_repair.flow/**}`, and
    `f/hermes_flow/candidate_ops/promotion.flow/**`) has a harder
    requirement: `f/hermes_flow/candidates/` (HF-011's candidate namespace —
    proposed capabilities awaiting promotion, deliberately Windmill-only)
    shares that same root. A `f/hermes_flow/**` wildcard would sweep
    candidates straight into git the moment one exists, defeating the
    entire point of a review-before-promotion gate — there'd be no
    meaningful difference between "candidate" and "active" if both landed
    in version control identically. `excludes: ["f/hermes_flow/candidates/**"]`
    backs this up defensively, but the real protection is that `includes`
    never lists anything under `candidates/` in the first place — notably,
    HF-011's own *code that writes candidates* deliberately lives at the
    sibling path `f/hermes_flow/candidate_ops/`, not inside
    `f/hermes_flow/candidates/` itself, precisely so that code stays
    syncable without colliding with the excluded data it manages. Add each
    new script here to `includes` by name — never widen this entry to a
    wildcard.
  - `f/workflows/` is likewise enumerated. HF-027 adds only the folder metadata
    and `product_collection.flow/**`; future composed workflows must add their
    own specific flow path and documentation instead of silently widening the
    mirror blast radius.
- **`skipSecrets: true`** — secret variables are invisible to sync in both
  directions. Pull writes a placeholder, never the real value.

## Scenario 1 — `make windmill-push` (repo → server)

The repo is the source of truth; the server is made to match it, **within scope**.

| Item | What happens |
|---|---|
| `f/hermes`, `f/collection`, `f/libraries`, or `f/capabilities` item in repo **and** server | server version **overwritten** |
| `f/hermes`, `f/collection`, `f/libraries`, or `f/capabilities` item in repo, **not** on server | **created** on server |
| `f/hermes`, `f/collection`, `f/libraries`, or `f/capabilities` item on server, **not** in repo | **removed** — scripts **archived** (recoverable), resources/variables/folders **hard-deleted** — *the dry-run guard aborts here unless `FORCE=1`* |
| `f/data_platform/` or `f/hermes_flow/` item **named in `includes`**, in repo **and** server | server version **overwritten** |
| `f/data_platform/` or `f/hermes_flow/` item **named in `includes`**, in repo, **not** on server | **created** on server |
| `f/data_platform/` or `f/hermes_flow/` item **named in `includes`**, on server, **not** in repo | **removed** (archived/hard-deleted per type) — dry-run guard applies, same as above |
| `f/data_platform/` or `f/hermes_flow/` item **not named in `includes`** (server or local) | **untouched** — out of scope regardless of which side it's on |
| `f/hermes_flow/repair/` explicitly tracked inspection/generation/fixture/adaptive-repair assets | created / overwritten / removed with the same guarded mirror semantics; unenumerated repair assets and `f/hermes_flow_state/adaptive_repair/**` attempt records remain untouched |
| `f/workflows/` item **named in `includes`** | created / overwritten / removed to mirror the repo; the dry-run deletion guard applies |
| `f/workflows/` item **not named in `includes`** | **untouched** — out of scope |
| **`f/hermes_flow/candidates/**`** (any candidate, server or local) | **untouched** — never in `includes`, additionally blocked by `excludes` |
| `folder.meta.yaml` (any tracked folder) | tracked → pushed (folder perms preserved); untracked → deleted |
| `hermes_endpoint` resource-type | created / overwritten |
| `f/collection/collection_db` (uses the inherited `postgresql` type) | created / overwritten — no resource-type push needed |
| **`f/hermes_state/**`** | **untouched** — out of scope |
| any other `f/*` folder | **untouched** — out of scope |
| `u/*` | **untouched** |
| inherited resource-types | **untouched** |
| secret variables | **untouched** (patched separately by `windmill-push`'s own curl calls, not by sync — see below) |

## Scenario 2 — `make windmill-pull` (server → repo)

The server is the source of truth; your working tree is made to match it. Safer,
because git is the safety net — review `git diff` before committing.

| Item | What happens |
|---|---|
| `f/hermes`, `f/collection`, `f/libraries`, or `f/capabilities` on server **and** in repo | repo file **overwritten** |
| `f/hermes`, `f/collection`, `f/libraries`, or `f/capabilities` on server, **not** in repo | **written** into the matching `windmill/f/.../` folder |
| `f/hermes`, `f/collection`, `f/libraries`, or `f/capabilities` in repo, **not** on server | **deleted** from working tree (`git checkout` restores) |
| `f/data_platform/` or `f/hermes_flow/` item **named in `includes`**, on server **and** in repo | repo file **overwritten** |
| `f/data_platform/` or `f/hermes_flow/` item **named in `includes`**, on server, **not** in repo | **written** into `windmill/f/data_platform/` or `windmill/f/hermes_flow/` |
| `f/data_platform/` or `f/hermes_flow/` item **named in `includes`**, in repo, **not** on server | **deleted** from working tree (`git checkout` restores) |
| `f/data_platform/` or `f/hermes_flow/` item **not named in `includes`** (e.g. a script added on the server) | **not pulled** — stays server-only, never enters git, until you add it to `includes` |
| `f/hermes_flow/repair/` explicitly tracked inspection/generation/fixture/adaptive-repair assets | written / overwritten / deleted locally to mirror the server; unenumerated repair assets and `f/hermes_flow_state/adaptive_repair/**` attempt records are not pulled |
| `f/workflows/` item **named in `includes`** | written / overwritten / deleted locally to mirror the server |
| `f/workflows/` item **not named in `includes`** | **not pulled** — stays out of the repo |
| **any `f/hermes_flow/candidates/**` item** (e.g. HF-011 creating a candidate directly on the server) | **not pulled** — stays server-only, never enters git, by design, not by omission — this is the entire point of the exclusion; verified live: a script created at `f/hermes_flow/candidates/hf007_probe` directly via the Windmill API never appeared under `windmill/f/hermes_flow/` after a real `wmill sync pull` |
| `hermes_endpoint` resource-type | written into `windmill/` |
| **`f/hermes_state/**`** | **not pulled** — stays server-only, never enters git |
| other `f/*`, `u/*`, inherited RTs | **not pulled** |
| secret variables under `f/hermes`, `f/collection`, `f/libraries`, `f/capabilities`, `f/data_platform`, or `f/hermes_flow` | **placeholder** only (real value never written) |

## Scenario 3 — `make windmill-check` (read-only drift report)

Non-destructive. Requires a clean `windmill/` tree, pulls into it, diffs against
git, prints any drift, then reverts the pull. Exit code is non-zero on drift, so
it works as a pre-push or scheduled guard. Because it is scoped to `f/hermes/**`,
`f/collection/**`, `f/libraries/**`, `f/capabilities/**`, and the enumerated `f/data_platform/`/
`f/hermes_flow/`/`f/workflows/` items, `f/hermes_state`, `f/hermes_flow/candidates/`,
unenumerated `f/data_platform/`/`f/hermes_flow/`/`f/workflows/` items, and other folders
**never show as drift** — their divergence is intentional, not drift.
The explicitly enumerated `f/hermes_flow/repair/` metadata, models, inspection,
candidate-generation, fixture-promotion, adaptive orchestration, finalizer, and
approval-flow assets participate in drift checks; any other repair asset remains
out of scope. Sanitised fixture bytes remain in `/shared/artifacts`, and bounded
attempt records remain under `f/hermes_flow_state/adaptive_repair/`; both are
outside Windmill sync scope.
Verified live for the candidate case specifically: a script created directly
on the server at `f/hermes_flow/candidates/hf007_probe` did not appear as
drift and was not pulled — the only drift `make windmill-check` reported
was `f/hermes_flow/folder.meta.yaml`'s own `owners`/`extra_perms` (the
server stamps the pushing admin's user as owner, a cosmetic field
difference, not a scope leak).

## Scenario 4 — installer auto-push (`install.sh` / `install.py`)

Same as Scenario 1 (behind the same guard), **plus** it create-or-no-ops the
`f/hermes_state` folder so scripts have somewhere to write state.

- **Fresh install** → creates `f/hermes` code + empty `f/hermes_state` and
  `f/hermes_flow_state` folders;
  purely additive, so it proceeds.
- **Re-run with UI edits in `f/hermes`** → guard aborts if it would delete
  anything; reconcile with `make windmill-pull` or override with
  `WMILL_FORCE_PUSH=1`.
- **Re-run with work in `f/hermes_state` or any other folder** → never touched.

## Deletion semantics (when a push *does* remove something in scope)

- **Scripts → archived.** Content retained; recover via the Windmill UI
  (show archived → unarchive) or by re-deploying.
- **Resources, non-secret variables, folders → hard-deleted.** No undo without a
  Postgres backup (`make backup`).
- **Secret variables → never touched** regardless of direction.

## The dry-run guard

`make windmill-push` and the installer run `wmill sync push --dry-run` first and
**abort if the push would delete or archive anything**, printing what it would
remove. Override deliberately:

```sh
make windmill-push FORCE=1          # destructive mirror
WMILL_FORCE_PUSH=1 ./install.sh …   # same, for the installer
```

## CLI gotcha: branch-validation prompt can hang a terminal

`wmill sync push`/`pull` run a git-branch validation step that can throw up its
own interactive y/n prompt — separate from the "this will delete things"
confirmation that `--yes` covers. If you run `wmill sync push`/`pull` by hand
without `--skip-branch-validation`, the command can sit silently waiting on
stdin for that prompt, looking like a hang. `windmill-push`/`-pull`/`-check`
all pass `--skip-branch-validation` to the underlying `wmill sync` calls for
this reason — do the same if you invoke `wmill` directly.

## Convention: where variables live

This is **load-bearing** — the protection above only holds if scripts follow it:

- **Secret** (API keys, tokens) → a **secret variable** (anywhere; `skipSecrets`
  keeps it out of git). The real value is set server-side, never committed.
- **Non-secret runtime state** (last-run timestamps, cursors, sync markers) →
  under a sibling `<folder>_state/` folder (e.g. `f/hermes_state/karakeep_last_run`).
  Never put it inside a tracked folder (`f/hermes/`, `f/collection/`,
  `f/libraries/`, `f/capabilities/`, or the enumerated `f/data_platform/`/`f/hermes_flow/`/`f/workflows/`
  items) — there it is treated as tracked config and a mirror push will
  delete it.
- **Proposed-but-not-yet-promoted capability code** → `f/hermes_flow/candidates/`
  (HF-011), never a tracked folder. This is a stronger rule than "runtime
  state" above: a candidate isn't state, it's real code, but it must stay
  server-only until promoted precisely so promotion means something —
  landing it in git the moment it's created would erase the distinction
  between "proposed" and "active."
- **Code/config that must be versioned** → under `f/hermes/`, `f/collection/`,
  `f/libraries/`, `f/capabilities/`, or — for `f/data_platform/`/`f/hermes_flow/`/`f/workflows/` — named
  explicitly in `includes` (see
  [Folder layout & sync scope](#folder-layout--sync-scope) above).

## Practical workflow

1. **Authoring in the UI?** → `make windmill-pull`, review `git diff`, commit.
2. **Authoring in the repo?** → `make windmill-check` first, then `make windmill-push`.
3. **Unsure?** → `make windmill-check` before either direction.

## Windmill MCP registration (`make windmill-mcp`)

Separate from asset sync above: this is how **Hermes reaches Windmill at
runtime** as a tool provider, not how `windmill/` assets get versioned.
Windmill ships a native Streamable-HTTP/SSE MCP server
(`/api/mcp/w/main/sse`, ~38 `x-mcp-tool` operations); see
[`architecture/adr/0005-hermes-windmill-transport.md`](../architecture/adr/0005-hermes-windmill-transport.md)
for why that transport was chosen over a bespoke HTTP tool.

`make windmill-mcp` productizes what used to be undocumented, hand-run
plumbing:

1. **Idempotency guard first.** If Hermes already has a `windmill` MCP entry
   registered *and* `hermes mcp test windmill` succeeds, the target leaves it
   completely alone and exits — it never silently swaps out a working
   connection's token or re-registers over it. (To rotate an existing
   connection onto the narrower token this target mints, remove it yourself
   first: `docker exec hermes hermes mcp remove windmill`, then re-run.)
2. **Token: reuse, then mint.** If `WM_MCP_TOKEN` is already set in `.env`
   and still authenticates, it's reused as-is. Otherwise the target logs in
   as the default Windmill admin (`admin@windmill.dev` / `changeme` — same
   pattern as `windmill-push`/`-pull`/`-check`) and mints a new token via
   `/api/users/tokens/create`, scoped to:
   `mcp:all`, `scripts:read`, `flows:read`, `jobs:read`, `jobs:run:scripts`,
   `jobs:run:flows`. The token is persisted to `.env` as `WM_MCP_TOKEN`
   (newline-safe, same `envput` pattern as `baserow-mcp`).

   `mcp:all` looks broad but isn't optional: Windmill gates the MCP
   endpoint itself (`/api/mcp/w/main/sse`) behind a separate `mcp:*`
   scope family (`mcp:all`, `mcp:favorites`, `mcp:scripts`, `mcp:flows`, …)
   independent of the per-operation REST scopes — a token with only
   `scripts:read`/`jobs:run:scripts`/etc. and no `mcp:*` scope gets a flat
   `403 Permission denied: Access denied. Required scope: mcp:*` before it
   ever reaches a single tool. Narrower `mcp:` values were tried
   (`mcp:scripts` + `mcp:flows`) and connect but currently report **zero**
   tools, so `mcp:all` is the only value observed to actually expose the
   tool set. This does *not* widen what the token can do, though: `mcp:all`
   only unlocks *visibility* into all ~38–43 MCP tools (including
   write-shaped ones like `createScript`/`createVariable`/`deleteFlowByPath`)
   — invoking one still hits Windmill's normal per-operation REST
   authorization underneath, which the token's other scopes gate as usual.
   Verified directly: with this exact scope set, `POST
   /api/w/main/scripts/create` and `POST /api/w/main/variables/create`
   both return `403` (no `scripts:write`/`variables:write`), while `GET
   /api/w/main/scripts/list` returns `200` (has `scripts:read`). So Hermes
   can *see* write tools in its tool list but any call to one fails closed
   — net effect matches the "Hermes never mutates active code" boundary in
   [`architecture/adr/0001-windmill-exclusive-execution.md`](../architecture/adr/0001-windmill-exclusive-execution.md),
   just enforced at call-time rather than by hiding the tool.
3. **Register with Hermes and verify.** Registers via
   `hermes mcp add windmill --url http://windmill_server:8000/api/mcp/w/main/sse --auth header`
   (the CLI prompts interactively for the token rather than taking it as a
   flag — the target pipes the y/token/y sequence over stdin), then confirms
   with `hermes mcp test windmill`.

Re-running `make windmill-mcp` on a host that already has a healthy
connection is a no-op (step 1); on a fresh install it mints and wires up a
narrowly-scoped token end to end.
