# Windmill sync — scope, scenarios, and conventions

This explains exactly what `wmill sync` does to your content, so a push never
surprises you. The golden rule:

> **`push` and `pull` are one-way *mirrors*, not merges.** Each makes its target
> match its source — which means each can *delete* content on its target side.
> `push` puts **server** content at risk; `pull` puts **repo** content at risk
> (but git can restore it).

## Folder layout & sync scope

Sync scope is set by `includes`/`excludes` in [`wmill.yaml`](wmill.yaml) — **not**
by folder boundaries. It is deliberately narrow:

| Path | Purpose | Versioned in repo? | In sync scope? |
|---|---|---|---|
| `f/hermes/**` | Hermes↔Windmill **code/config** | ✅ yes | ✅ yes |
| `f/collection/**` | collection_db Postgres resource + Baserow webhook receiver | ✅ yes | ✅ yes |
| `hermes_endpoint` resource-type | the shared endpoint type | ✅ yes | ✅ yes |
| `f/hermes_state/**` | **runtime state** (timestamps, cursors, non-secret vars) | ❌ no | ❌ **no** |
| any other `f/*` folder | unrelated projects | ❌ no | ❌ no |
| all `u/*` (incl. a future `u/hermes`) | user namespaces | ❌ no | ❌ no |
| inherited resource-types (the ~250 from `admins`, incl. the built-in `postgresql` type used by `f/collection/collection_db`) | Windmill built-ins | ❌ no | ❌ no |
| secret variables (anywhere) | credentials | ❌ no (placeholder only) | ❌ no (`skipSecrets`) |

Two settings enforce this:

- **`includes: ["f/hermes/**", "f/collection/**", "hermes_endpoint.resource-type.yaml"]`**
  — the Hermes and collection code folders, plus the one custom resource-type
  (`f/collection/collection_db` uses the built-in `postgresql` type, so it needs
  no resource-type file of its own). The glob is separator-aware, so
  `f/hermes/**` does **not** match `f/hermes_state` or any other folder, and
  `f/collection/**` does not pull in any unrelated `f/*` folder either.
- **`skipSecrets: true`** — secret variables are invisible to sync in both
  directions. Pull writes a placeholder, never the real value.

## Scenario 1 — `make windmill-push` (repo → server)

The repo is the source of truth; the server is made to match it, **within scope**.

| Item | What happens |
|---|---|
| `f/hermes` or `f/collection` item in repo **and** server | server version **overwritten** |
| `f/hermes` or `f/collection` item in repo, **not** on server | **created** on server |
| `f/hermes` or `f/collection` item on server, **not** in repo | **removed** — scripts **archived** (recoverable), resources/variables/folders **hard-deleted** — *the dry-run guard aborts here unless `FORCE=1`* |
| `folder.meta.yaml` (either folder) | tracked → pushed (folder perms preserved); untracked → deleted |
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
| `f/hermes` or `f/collection` on server **and** in repo | repo file **overwritten** |
| `f/hermes` or `f/collection` on server, **not** in repo | **written** into the matching `windmill/f/.../` folder |
| `f/hermes` or `f/collection` in repo, **not** on server | **deleted** from working tree (`git checkout` restores) |
| `hermes_endpoint` resource-type | written into `windmill/` |
| **`f/hermes_state/**`** | **not pulled** — stays server-only, never enters git |
| other `f/*`, `u/*`, inherited RTs | **not pulled** |
| secret variables under `f/hermes` or `f/collection` | **placeholder** only (real value never written) |

## Scenario 3 — `make windmill-check` (read-only drift report)

Non-destructive. Requires a clean `windmill/` tree, pulls into it, diffs against
git, prints any drift, then reverts the pull. Exit code is non-zero on drift, so
it works as a pre-push or scheduled guard. Because it is scoped to `f/hermes/**`
and `f/collection/**`, `f/hermes_state` and other folders **never show as
drift** — their divergence is intentional, not drift.

## Scenario 4 — installer auto-push (`install.sh` / `install.py`)

Same as Scenario 1 (behind the same guard), **plus** it create-or-no-ops the
`f/hermes_state` folder so scripts have somewhere to write state.

- **Fresh install** → creates `f/hermes` code + an empty `f/hermes_state` folder;
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

## Convention: where variables live

This is **load-bearing** — the protection above only holds if scripts follow it:

- **Secret** (API keys, tokens) → a **secret variable** (anywhere; `skipSecrets`
  keeps it out of git). The real value is set server-side, never committed.
- **Non-secret runtime state** (last-run timestamps, cursors, sync markers) →
  under **`f/hermes_state/`** (e.g. `f/hermes_state/karakeep_last_run`). Never put
  it in `f/hermes/` — there it is treated as tracked config and a mirror push will
  delete it.
- **Code/config that must be versioned** → under **`f/hermes/`**.

## Practical workflow

1. **Authoring in the UI?** → `make windmill-pull`, review `git diff`, commit.
2. **Authoring in the repo?** → `make windmill-check` first, then `make windmill-push`.
3. **Unsure?** → `make windmill-check` before either direction.
