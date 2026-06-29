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
| `f/data_platform/{folder.meta,data_platform_db.resource,db_password.variable,dbt_run.*,extract_hn_stories.*}` | dlt/dbt pipeline scripts + their resource/secret | ✅ yes | ✅ yes (named explicitly, not wildcarded) |
| `hermes_endpoint` resource-type | the shared endpoint type | ✅ yes | ✅ yes |
| `f/hermes_state/**` | **runtime state** (timestamps, cursors, non-secret vars) | ❌ no | ❌ **no** |
| any other item dropped into `f/data_platform/` not named above | unenumerated pipeline assets | n/a | ❌ **no** |
| any other `f/*` folder | unrelated projects | ❌ no | ❌ no |
| all `u/*` (incl. a future `u/hermes`) | user namespaces | ❌ no | ❌ no |
| inherited resource-types (the ~250 from `admins`, incl. the built-in `postgresql` type used by `f/collection/collection_db`) | Windmill built-ins | ❌ no | ❌ no |
| secret variables (anywhere) | credentials | ❌ no (placeholder only) | ❌ no (`skipSecrets`) |

Two settings enforce this:

- **`includes`** — see [`wmill.yaml`](wmill.yaml) for the live list. `f/hermes/**`
  and `f/collection/**` are wholesale wildcards; the glob is separator-aware, so
  neither matches `f/hermes_state`, the other folder, or any unrelated `f/*`.
  `f/data_platform/` is different: its entries name each file/pattern
  explicitly (`f/data_platform/dbt_run.*`, `f/data_platform/extract_hn_stories.*`,
  plus the folder's `folder.meta.yaml`, resource, and secret variable) instead
  of `f/data_platform/**`. That's deliberate — it's the folder most likely to
  grow extra scripts/flows/apps over time (on the server or locally), and a
  wildcard there would silently widen the mirror's blast radius every time
  someone adds something without updating this doc. Adding a new pipeline to
  `f/data_platform/` means adding its files to `includes` explicitly; see
  [`docs/data-platform-add-pipeline.md`](data-platform-add-pipeline.md).
- **`skipSecrets: true`** — secret variables are invisible to sync in both
  directions. Pull writes a placeholder, never the real value.

## Scenario 1 — `make windmill-push` (repo → server)

The repo is the source of truth; the server is made to match it, **within scope**.

| Item | What happens |
|---|---|
| `f/hermes` or `f/collection` item in repo **and** server | server version **overwritten** |
| `f/hermes` or `f/collection` item in repo, **not** on server | **created** on server |
| `f/hermes` or `f/collection` item on server, **not** in repo | **removed** — scripts **archived** (recoverable), resources/variables/folders **hard-deleted** — *the dry-run guard aborts here unless `FORCE=1`* |
| `f/data_platform/` item **named in `includes`**, in repo **and** server | server version **overwritten** |
| `f/data_platform/` item **named in `includes`**, in repo, **not** on server | **created** on server |
| `f/data_platform/` item **named in `includes`**, on server, **not** in repo | **removed** (archived/hard-deleted per type) — dry-run guard applies, same as above |
| `f/data_platform/` item **not named in `includes`** (server or local) | **untouched** — out of scope regardless of which side it's on |
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
| `f/hermes` or `f/collection` on server **and** in repo | repo file **overwritten** |
| `f/hermes` or `f/collection` on server, **not** in repo | **written** into the matching `windmill/f/.../` folder |
| `f/hermes` or `f/collection` in repo, **not** on server | **deleted** from working tree (`git checkout` restores) |
| `f/data_platform/` item **named in `includes`**, on server **and** in repo | repo file **overwritten** |
| `f/data_platform/` item **named in `includes`**, on server, **not** in repo | **written** into `windmill/f/data_platform/` |
| `f/data_platform/` item **named in `includes`**, in repo, **not** on server | **deleted** from working tree (`git checkout` restores) |
| `f/data_platform/` item **not named in `includes`** (e.g. a script added on the server) | **not pulled** — stays server-only, never enters git, until you add it to `includes` |
| `hermes_endpoint` resource-type | written into `windmill/` |
| **`f/hermes_state/**`** | **not pulled** — stays server-only, never enters git |
| other `f/*`, `u/*`, inherited RTs | **not pulled** |
| secret variables under `f/hermes`, `f/collection`, or `f/data_platform` | **placeholder** only (real value never written) |

## Scenario 3 — `make windmill-check` (read-only drift report)

Non-destructive. Requires a clean `windmill/` tree, pulls into it, diffs against
git, prints any drift, then reverts the pull. Exit code is non-zero on drift, so
it works as a pre-push or scheduled guard. Because it is scoped to `f/hermes/**`,
`f/collection/**`, and the enumerated `f/data_platform/` items, `f/hermes_state`,
unenumerated `f/data_platform/` items, and other folders **never show as
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
  Never put it inside a tracked folder (`f/hermes/`, `f/collection/`, or the
  enumerated `f/data_platform/` items) — there it is treated as tracked config
  and a mirror push will delete it.
- **Code/config that must be versioned** → under `f/hermes/`, `f/collection/`,
  or — for `f/data_platform/` — named explicitly in `includes` (see
  [Folder layout & sync scope](#folder-layout--sync-scope) above).

## Practical workflow

1. **Authoring in the UI?** → `make windmill-pull`, review `git diff`, commit.
2. **Authoring in the repo?** → `make windmill-check` first, then `make windmill-push`.
3. **Unsure?** → `make windmill-check` before either direction.
