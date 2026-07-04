# HermesFlow — Agent Guidelines

This file tells AI coding agents (Claude Code, Copilot, etc.) about the
conventions that must be followed when modifying this repository. Read it
before touching any file.

---

## Stack overview

| Layer | Services |
|---|---|
| Ingress | `caddy` |
| Agent | `hermes`, `headroom` |
| Memory | `hindsight`, `hindsight_db` |
| Inference | `ollama` |
| Workflow | `windmill_server`, `windmill_lsp`, `windmill_worker` (×2), `windmill_worker_native` |
| Data | `db` (PostgreSQL 16) |
| Observability (optional, `docker-compose.observability.yml`) | `prometheus`, `alertmanager`, `grafana`, `cadvisor`, `node_exporter`, `postgres_exporter`, `collection_postgres_exporter`, `hindsight_postgres_exporter`, `loki`, `promtail` |

Networks: `backend`, `edge`, `agent`, `memory`, `inference`, `monitoring`.

---

## Installers & first-run

There are two non-interactive installers plus the interactive `make bootstrap`:

| Path | Notes |
|---|---|
| `install.sh` | bash; uses `make`, `openssl`, `curl`. |
| `install.py` | pure Python **standard library only** (Windows-friendly — no `make`/`bash`/`openssl`/`curl`). |
| `make bootstrap` | interactive Hermes setup wizard. |
| `INSTALL.md` | step-by-step walkthrough of the installers. |

Hard rules when touching the installers:

- **Keep `install.sh` and `install.py` at parity.** Same flags, same steps, same
  behaviour. A change to one must be mirrored in the other (and in the
  `--help`/docstring header), or don't make it.
- **`install.py` stays stdlib-only.** No `pip` dependencies — it must run on a
  fresh Windows host with just Python 3 + Docker Desktop.
- **Both are idempotent.** Re-running fills blanks only; never clobber existing
  secrets or user-set values.
- **Never run the real installer to "test" it** — it writes `.env`, creates
  dirs, pulls images, and starts/restarts containers. Validate with
  `--dry-run`, `bash -n install.sh`, and `python3 -m py_compile install.py`.
  `--dry-run` prints the resolved plan and exits before any side effect; it must
  stay genuinely side-effect-free.
- **Profiles** (`--profile minimal|full|gpu|mac|server|remote`) are presets that
  seed flag defaults *before* explicit flags, so explicit flags always win. Keep
  the preset table identical in both installers.
- Steps live in a fixed order (prereqs → model check → `.env` → secrets → data
  dirs → keys → up → probe → memory → windmill → mlx → headroom). New work slots
  into that sequence; reflect it in the header comment and `INSTALL.md`.

When you add or change installer behaviour, update **both** `README.md` (Quick
start / flags) and `INSTALL.md` (flag table + step description).

---

## Secrets & credential handling

- **Required secrets are auto-generated, never left to a weak compose default.**
  `make secrets` (and `install.py`'s `ensure_secret` calls) generate
  `API_SERVER_KEY`, `WM_DB_PASSWORD`, `HINDSIGHT_DB_PASSWORD`,
  `GRAFANA_ADMIN_PASSWORD` when blank or still a known-weak default. If you add a
  service that needs a password, add it to **both** places — do **not** ship a
  `${FOO:-changeme}` default in `docker-compose.yml` as the only protection.
- **Hermes reads provider keys and bot tokens from `<DATA_DIR>/.env`.** The
  `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
  `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` lines in the `hermes` service are
  intentionally **commented out** so they come from the wizard/installer-written
  `<DATA_DIR>/.env` (mapped to `/opt/data/.env`). Write Hermes-consumed secrets
  there, `chmod 600`.
- **The provider key must ALSO go in the top-level `.env`.** Other services
  substitute it directly — `headroom` uses `${OPENROUTER_API_KEY}` and remote
  Hindsight uses `${HINDSIGHT_LLM_API_KEY}`. If the key is only in
  `<DATA_DIR>/.env`, those come up blank and stay blank across redeploys. The
  installers write the provider key to **both** files; keep it that way.
- **Never write a real secret into a tracked file.** `windmill/f/hermes/
  api_key.variable.yaml` keeps a placeholder; the real value is set server-side
  (UI/API), and `wmill.yaml` keeps `skipSecrets: true`. `.env`, `<DATA_DIR>`, and
  `backups/` are git-ignored — keep real keys only there.
- Generating a DB password only helps **before** the volume is initialized —
  Postgres applies `POSTGRES_PASSWORD` once, on first init. Rotating it later
  breaks auth. Installers therefore generate secrets before the first `up`.

---

## Windmill assets & sync conventions

`wmill sync` is a **mirror** (`push` makes the server match the repo and
**deletes/archives** anything in scope that isn't tracked). Scope is set by
`includes` in `windmill/wmill.yaml` and is deliberately narrow — currently
`f/hermes/**` + `f/collection/**` + an explicit, item-by-item list of
`f/data_platform/` files + the `hermes_endpoint` resource-type. Full
breakdown in [`docs/windmill-sync.md`](docs/windmill-sync.md). When you author or
generate a Windmill script, follow these rules so a push can never wipe data:

- **Never hand someone (or run yourself) a bare `wmill sync push`/`pull`
  command without `cd windmill &&` baked into the same command line.**
  Without `windmill/wmill.yaml` loaded, the CLI has zero scope restriction
  and mirrors the *entire* remote workspace against whatever's in the
  current directory — see the `[!WARNING]` block at the top of
  [`docs/windmill-sync.md`](docs/windmill-sync.md) for the incident this
  caused (every secret/resource/folder in the workspace hard-deleted,
  several scripts never tracked in this repo archived). Always prefer
  `make windmill-push`/`-pull`/`-check`, which `cd windmill` for you and
  (push) dry-run-and-abort on any deletion.

- **Code/config that must be versioned → one of the synced folders** (currently
  `f/hermes/`, `f/collection/`, and `f/data_platform/`). Commit it; it
  round-trips via `make windmill-push` / `make windmill-pull`.
- **`f/data_platform/` is scoped item-by-item, not `f/data_platform/**`.**
  Unlike `f/hermes/` and `f/collection/`, that folder's `includes` entries
  name each file/pattern explicitly (e.g. `f/data_platform/dbt_run.*`). This
  is deliberate: a blanket wildcard would sweep up any script/flow/app
  someone adds to that folder later — on the server or locally — into the
  mirror's blast radius. When you add a new pipeline's assets under
  `f/data_platform/`, add their specific filenames/patterns to `includes`
  rather than relying on the folder already being in scope.
- **Non-secret runtime state → a sibling `<folder>_state/` folder, never inside
  the synced folder itself.** Any variable a script *writes at runtime* — last-run
  timestamps, cursors, sync markers, paging state — goes under `f/hermes_state/`
  (e.g. `f/hermes_state/<thing>_last_run`) for Hermes-owned scripts, or the
  equivalent `<folder>_state/` sibling for any other synced folder. That sibling
  is **outside sync scope**, so it is never versioned and a mirror push never
  deletes it. Putting state inside a synced folder is a bug: the next push
  deletes it (this is exactly how `f/hermes/karakeep_last_run` was lost). If a
  new synced folder needs runtime state, create its `<folder>_state/` sibling and
  have the installer/Makefile target create-or-no-op it, mirroring
  `f/hermes_state`.
- **Secrets → a secret variable, value set server-side.** Never write a real key
  into a tracked file; `skipSecrets: true` keeps secret variables out of sync
  entirely (see the secrets section above). The placeholder
  `f/hermes/api_key.variable.yaml` (and `f/collection/db_password.variable.yaml`)
  is the pattern — commit the placeholder, patch the real value via a
  `variables/create`-or-`variables/update` API call in the relevant
  `make <feature>-push`-style target (see `windmill-push` for both examples).
- **Prefer a built-in resource type over a custom one.** `f/collection/collection_db`
  uses Windmill's inherited `postgresql` type — no `*.resource-type.yaml` needed.
  Only add a custom resource-type file (like `hermes_endpoint`) when no built-in
  type fits; custom resource-types must also be added to `includes` explicitly.
- **Everything else (`u/*`, other untracked `f/*` folders) is out of scope by
  design.** If a script provisions, say, a `u/hermes` service user, that is a
  runtime/admin action — do **not** add it to `includes` or expect sync to manage it.
- **Adding a new tracked Windmill folder (e.g. for a new integration) is a
  three-file change, not just dropping files in `windmill/f/<name>/`:**
  1. Add `"f/<name>/**"` explicitly to `includes` in `windmill/wmill.yaml` (never
     widen to a blanket `f/**` — narrow, explicit scope is the safety mechanism).
     If the folder is one where extra items might show up unexpectedly (e.g. a
     pipeline folder others will add scripts to over time, on the server or
     locally), enumerate the specific files/patterns instead of the folder
     wildcard — see the `f/data_platform/` entries above for the pattern.
  2. Add a row for it to the scope table and the three scenario tables in
     `docs/windmill-sync.md`.
  3. Commit the folder's `folder.meta.yaml` (generate with
     `wmill folder add-missing`) so pushes don't strip folder permissions.
  Forgetting step 1 is a silent no-op, not an error: `wmill sync push` will
  simply never push the new folder's resources/scripts/variables, and
  `windmill-push`'s own secret-patching curl calls can succeed even though the
  resource/script they're patching alongside was never actually deployed.
- **Don't widen `includes` casually** beyond what step 1 above requires. Narrow
  scope is the safety mechanism; a push's blast radius is whatever `includes`
  covers.

---

## Optional features = compose overrides, not edits to the base file

Optional capabilities are layered with a `docker-compose.<feature>.yml` override
toggled via `COMPOSE_FILE` in `.env`, leaving the default stack untouched. Docker
Compose reads `COMPOSE_FILE` from `.env`, so every `docker compose` / `make` call
picks the override up. Prefer this over uncommenting blocks in the base file.

Three reference points, minimal → full:

- **`docker-compose.gpu.yml`** — one-service tweak: adds the NVIDIA device
  reservation to `ollama` (`--gpu`).
- **`docker-compose.baserow.yml`** — a full subsystem with its own dedicated
  Postgres/Redis: an extra app, its own network, a Caddy route, secrets, an
  installer flag, Makefile lifecycle targets, and an agent (MCP) bootstrap.
- **`docker-compose.directus.yml`** — a full subsystem that instead **shares**
  the base stack's `collection_db` Postgres (see
  [Shared `collection_db` schema isolation](#shared-collection_db-schema-isolation)
  below): an extra app, secrets, an installer flag (`--with-directus`), and
  Makefile lifecycle targets (`make directus` / `directus-revert`) — but no
  MCP bootstrap target, since its MCP server is enabled natively in the
  Directus Studio UI rather than bridged through Hermes.

### Shared `collection_db` schema isolation

Unlike Baserow (its own dedicated Postgres container), Directus reuses the
base stack's `collection_db` Postgres instance, alongside Windmill's
"collection" role. `collection_db/initdb/01-init.sh` creates three isolated
schemas in one physical database, each with its own role:

- `baserow` — Baserow's own private schema. No other role gets access; its
  table/field DDL is internally managed and direct external writes risk
  corrupting it. Any Baserow ↔ collection sync goes through Baserow's
  webhooks, not SQL.
- `directus` — Directus's own system tables (`directus_users`,
  `directus_permissions`, etc).
- `collection` — shared business data (page scrapes, LLM generations, triage
  records), writable by both `directus` and `windmill_collection` roles, but
  **not** by `baserow`.

If you add another subsystem that needs to share this database, follow the
same pattern: a dedicated role + schema in `01-init.sh`, scoped grants on
`collection` only if it genuinely needs to read/write shared data.

`data_platform` is the existing example: a dlt/dbt pipeline stack that
extracts to immutable Parquet under `${SHARED_DIR}/datalake/`, stages in an
ephemeral per-job DuckDB instance, and writes mart models into its own
`data_platform` schema in `collection_db` via dbt-duckdb's Postgres attach
feature — no extra Postgres container. Architecture and rationale:
[docs/plans/datalake.md](docs/plans/datalake.md). **Adding a new pipeline
to it is a documented checklist, not a from-scratch design exercise:**
[docs/data-platform-add-pipeline.md](docs/data-platform-add-pipeline.md) —
follow it in order; it exists because most of the steps in it were
failure modes hit once already (wrong dbt-duckdb external-source syntax,
schema-naming gotchas, missing raw-layer dedupe, lock files that look
right but break at runtime).

### `COMPOSE_FILE` is additive

Overrides must compose, so `--gpu` and `--with-baserow` can both be on at once.
Build `COMPOSE_FILE` from `docker-compose.yml` + each enabled override,
**idempotently — never overwrite it**. Both installers share a `compose_add`
helper for this; the Makefile `make <feature>` / `<feature>-revert` targets
add/remove their own override from the list.

### Wiring checklist (per optional extension)

Use only the layers the feature needs — `gpu` uses just the first two, `baserow`
uses all of them. Keep `install.sh` and `install.py` at parity throughout.

1. **Override file** `docker-compose.<feature>.yml` — its services still follow
   the [Adding a new container](#adding-a-new-container) checklist.
2. **Installer flag** `--with-<feature>` (default off) → `compose_add` the
   override; reflect it in the usage text and the `--dry-run` summary.
3. **Makefile lifecycle** — `make <feature>` (generate secrets → add the override
   to `COMPOSE_FILE` → `up -d` the services) and `make <feature>-revert`
   (stop/`rm` the services → drop the override; volumes preserved). Add both to
   `.PHONY` with `## ` help comments.
4. **Secrets** — `ensure_secret` calls in `make secrets` **and** the matching
   `install.py`; leave them blank with `# <REQUIRED>` in `.env.example` (never a
   weak default in the override). See [Secrets & credential handling](#secrets--credential-handling).
5. **Backup** — a *guarded* dump in `make backup` (only when the service is
   running, since the feature is usually off).
6. **CI** — add the merged config to the compose job:
   `docker compose -f docker-compose.yml -f docker-compose.<feature>.yml config -q`
   (the base CI run doesn't include overrides). `gpu`, `baserow`, and
   `directus` are all covered
7. **Docs** — a README section + an INSTALL flag-table row; add a focused
   `docs/<feature>*.md` for deeper caveats when warranted.

### Override-file gotchas

- **YAML anchors are file-scoped.** The base `&default-logging` anchor is not
  visible in an override — redefine `x-logging: &default-logging` at the top of
  any override that uses it.
- **New networks** go in the override's own `networks:`; base networks (`edge`,
  `agent`, …) merge across files and can just be referenced.
- **Never reference an optional service from the base file** (e.g. in `caddy`'s
  `depends_on`) — it breaks `docker compose config` when the override is absent. A
  Caddy route in the always-present `Caddyfile` is fine: it just 502s until the
  service is up.
- **Optional exporters vs. Prometheus.** This *qualifies* the observability step
  of the new-container checklist: cAdvisor covers every container automatically,
  but a *static* scrape target for an optional service trips the `up == 0`
  PrometheusTargetDown alert for everyone running without the override. Don't add
  static scrape jobs for optional exporters — rely on cAdvisor and note the gap
  (see the comment in `docker-compose.baserow.yml`).
- **`.env` writers must be newline-safe.** Appending `KEY=VALUE` to a file whose
  last line has no trailing newline concatenates onto it (it once mangled a
  password). `ensure_secret` and the `baserow-mcp` `envput` add a newline first —
  copy that guard for any new writer.

### Runtime / agent (MCP) bootstrap

Wiring that depends on a *running* service, an account, or credentials belongs in
a dedicated, idempotent `make <feature>-...` target, **not** install-time.
`make baserow-mcp` is the template: it provisions via the service's REST API,
persists generated values back to `.env` (newline-safe), reuses what already
exists on re-run, and verifies the result.

Exposing a service to Hermes over **MCP** has a transport catch: Hermes drives
Streamable-HTTP for `--url` servers. If the service only speaks the legacy
HTTP+SSE transport (e.g. Baserow), bake the bridge into the **hermes image**
(`mcp-remote`, see `hermes/Dockerfile`) and register it as a stdio server —
baking keeps startup within Hermes's connect deadline (runtime `npx` is too slow).

Not every integration needs this dance — if the service ships a native MCP
server already (Directus, v11.12+), there's no bridge to bake: enable it via
the app's own UI (Settings → AI in Directus's case) and register the
resulting endpoint/token with Hermes directly. Don't add a
`make directus-mcp`-style target for this; it's a one-time manual step, not
a repeatable provisioning flow like `baserow-mcp`.

> **GPU notes.** Docker Desktop on **macOS cannot pass the GPU** into containers,
> so the bundled `ollama` is CPU-only there (use host-native `mlx/` or remote
> inference). On a **Linux/NVIDIA** host, `--gpu` makes the in-container `ollama`
> GPU-accelerated.
>
> **Hindsight note.** The memory provider reads `HINDSIGHT_API_URL`; the
> `hindsight-client` package ships in the hermes image (no `pip install` needed —
> just set `memory.provider`).
>
> **Directus note.** No native "AI field" type or AI Flow operation exists.
> AI-assisted field generation needs a Directus Flow (Webhook/Request URL →
> Run Script → Update Data) calling Ollama's `/v1` endpoint over the
> `inference` network, or the built-in AI Assistant chat panel pointed at the
> same endpoint via Settings → AI.

---

## Adding a new container

Work through **every** section below in order. Each section is a hard
requirement, not a suggestion.

### 1. Assign the right networks

Connect the service only to the networks it genuinely needs:

| The service needs to… | Add network |
|---|---|
| Be reachable through Caddy | `edge` |
| Read from / write to the Windmill database | `backend` |
| Call the Hermes OpenAI-compatible API | `agent` |
| Read or write Hindsight memory | `memory` |
| Be scraped by Prometheus | `monitoring` |

Never attach a service to a network for convenience — each extra attachment
is an attack surface.

### 2. Wire up observability (required)

Every new service must be observable. cAdvisor already emits per-container
CPU / memory / network / disk metrics automatically, so container-level
health is covered without any action. You must still do the following:

#### 2a. Application metrics

If the image exposes a Prometheus `/metrics` endpoint (check the image docs):

1. Add the service to the `monitoring` network.
2. Add a scrape job to `prometheus/prometheus.yml`:

```yaml
- job_name: <service-name>
  static_configs:
    - targets: ['<container-name>:<metrics-port>']
```

If the image does **not** expose metrics natively but is a well-known
technology, check whether a sidecar exporter exists on
[exporterhub.io](https://exporterhub.io) or the Prometheus community org
(`prometheuscommunity/<name>-exporter`). Use the pattern already established
by `postgres_exporter`: put the exporter in a separate service, connect it
to both `monitoring` and whichever application network the target is on, and
add `depends_on` pointing at the target service.

If no exporter exists, document why in a comment next to the service in
`docker-compose.yml` so the gap is visible.

#### 2b. Grafana dashboard

After wiring the scrape job, add (or reference) a dashboard:

- **Community dashboard**: note the Grafana dashboard ID in the comment
  block at the top of the observability section in `docker-compose.yml`
  (see the existing IDs for cAdvisor: 193, Node Exporter: 1860, PostgreSQL:
  9628). A human will import it via the Grafana UI.
- **Custom dashboard**: save the JSON to
  `grafana/provisioning/dashboards/<service-name>.json`. Grafana will
  auto-load it on the next restart because the provisioning provider watches
  that directory.

### 3. Set resource limits

Every service must declare a `deploy.resources.limits` block. Use these
bands as a starting point and adjust based on the image's documented
requirements:

| Role | CPU | Memory |
|---|---|---|
| Heavy workload (workers, ML inference) | 1.0–2.0 | 1–4 G |
| Application server / API | 0.5–1.0 | 256–1024 M |
| Lightweight exporter / sidecar | 0.1–0.3 | 64–256 M |

### 4. Add a healthcheck

Add a `healthcheck` if the image exposes any health or readiness endpoint.
Use `curl -fsS` or `wget -qO-` against the documented path. Example:

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://localhost:<port>/health"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 40s
```

If there is genuinely no health endpoint, omit the block and add a comment
explaining why.

### 5. Add a Caddy route (if user-facing)

If the service has a UI or an API that should be reachable through the
reverse proxy:

1. Add it to the `edge` network.
2. Add a route to `Caddyfile`:

```
http://<name>.localhost {
    reverse_proxy <container-name>:<port>
}
```

3. For services that carry sensitive data or have no built-in auth, add a
   comment noting that `basicauth` should be considered before exposing to
   a non-trusted network.

### 6. Document environment variables

Add every configurable variable to `.env.example` with:
- A short comment explaining what it does
- A safe default value
- A `${VAR_NAME_DATA_DIR}` path variable if the service writes state, so
  the operator can redirect it alongside the other data directories

If the variable is a **secret** (password / token / key):
- Mark it `# <REQUIRED>` and leave the value blank in `.env.example`.
- Add it to `make secrets` (an `ensure_secret` call) **and** the matching
  `ensure_secret(...)` in `install.py` so it's auto-generated — see
  [Secrets & credential handling](#secrets--credential-handling). Never rely on a
  weak `${FOO:-changeme}` default in `docker-compose.yml`.
- If Hermes consumes it (provider key, bot token), it must be read from
  `<DATA_DIR>/.env`, not the top-level `.env`.

### 7. Use the shared logging anchor

All services must include:

```yaml
logging: *default-logging
```

This gives JSON log output with automatic rotation (20 MB / 5 files,
gzip-compressed). Do not override the logging driver unless there is a
documented operational reason.

---

## Modifying an existing service

- **Changing networks**: re-read section 1 and section 2a — removing a
  network may silently break a Prometheus scrape job.
- **Changing ports**: update `prometheus/prometheus.yml` if the service
  exposes metrics, and update `Caddyfile` if there is a proxy route.
- **Removing a service**: also remove its scrape job from
  `prometheus/prometheus.yml`, its Caddy route, and its `.env.example`
  entries.

---

## Adding Python packages for Hermes

Hermes does **not** run the upstream image directly — it runs a thin derived
image built from `hermes/Dockerfile` (the `hermes` service uses `build:`). This
exists because the upstream `nousresearch/hermes-agent` image disables runtime
lazy installs (`HERMES_DISABLE_LAZY_INSTALLS=1`) **and** ships a read-only,
root-owned venv, so the agent cannot pip-install at runtime — by design, to stop
a prompt-injected session from self-modifying its own venv.

To add a package:

1. Find its **exact pinned spec** in Hermes' `LAZY_DEPS` allowlist:
   `docker exec hermes grep -n -B2 '<package>' /opt/hermes/tools/lazy_deps.py`.
2. Add that spec to `hermes/requirements.txt` (it's baked into the venv at build
   time, then the venv is re-locked — runtime privilege is unchanged).
3. Redeploy: `docker compose build hermes && docker compose up -d hermes`.

The pin **must** match `LAZY_DEPS` so `ensure()` treats the feature as already
satisfied and never reaches the disabled install path. **Never** unset the
disable flag, chown/chmod the venv writable, or add a writable `PYTHONPATH`
dir — all weaken the security model. CI (`.github/workflows/hermes-image.yml`,
path-filtered to `hermes/**`) builds the image to validate every pin resolves.
Full rationale: `docs/hermes-docker-build.md`.

**Invariant — the baked venv is the single source of truth.** A `PYTHONPATH`
overlay under `/opt/data` (e.g. `/opt/data/.hermes-extras`, plus a `PYTHONPATH=`
line a self-healing agent session may write into `/opt/data/.env`) is **drift**,
never a fix: it sorts ahead of the venv on `sys.path` and shadows pinned,
CVE-patched core packages. Deploys neutralize it with `make hermes-heal`
(idempotent; run automatically by `bootstrap` and both installers). If you find
such an overlay, run `make hermes-heal` — don't sanction it.

**System/npm tools (tmux, claude-code, opencode) are baseline, not optional.**
`hermes/Dockerfile` also installs `tmux` (apt) and the `@anthropic-ai/claude-code`
/ `opencode-ai` CLIs (npm) — the bundled `claude-code` and `opencode` skills
assume these exist (the claude-code skill's interactive PTY mode requires tmux).
These install into system paths, not the venv, so they don't go through
`requirements.txt`/`LAZY_DEPS` and don't interact with `hermes-heal`. Auth is
manual (`claude` / `opencode auth login` inside the container, or API-key env
vars) — not baked into the image or compose file.

---

## Hermes custom skills

Custom skills (the Markdown playbooks Hermes's `skills_hub` routes to) live
in this repo at `hermes/skills/<category>/<skill-name>/` and deploy to the
Hermes-bound `DATA_DIR/skills/` via `make hermes-skills-push` — additive
only, same as Windmill sync, so it never touches Hermes's own bundled/curated
skills living alongside them. Pull live edits back for review with
`make hermes-skills-pull` (scoped only to skills already tracked here) before
trusting that a skill Hermes itself edited still matches what's committed.
Full guide, skill anatomy, and the audit-before-commit workflow:
[docs/hermes-skills.md](docs/hermes-skills.md).

---

## Observability reference

| What | Where |
|---|---|
| Grafana UI | `http://grafana.localhost` |
| Prometheus UI | `http://prometheus.localhost` |
| Alertmanager UI | `http://alertmanager.localhost` |
| Scrape config | `prometheus/prometheus.yml` |
| Alert rules | `prometheus/alert.rules.yml` |
| Alert routing / receivers | `alertmanager/alertmanager.yml` (no real receiver configured by default) |
| Log aggregation | Loki + Promtail — query via Grafana **Explore** → `Loki` datasource, no separate UI |
| Loki / Promtail config | `loki/loki-config.yml`, `loki/promtail-config.yml` |
| Datasource provisioning | `grafana/provisioning/datasources/prometheus.yml` |
| Dashboard auto-load directory | `grafana/provisioning/dashboards/` |
| Grafana credentials | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in `.env` |
| Metrics retention | `PROMETHEUS_RETENTION` in `.env` (default 15d) |

cAdvisor provides container-level metrics for **all** services automatically —
no per-service action is needed for CPU, memory, network, and disk I/O.

---

## Validation before committing

```bash
# Compose (mirrors CI): syntax + ${VAR} expansion against .env.example
make validate                       # = docker compose config --quiet
make ci                             # validate + lint (ruff + py_compile of windmill/)

# Confirm every service has resource limits
docker compose config | grep -A5 'deploy:' | grep -c 'cpus'

# Confirm every non-monitoring service has logging configured
docker compose config | grep 'driver: json-file' | wc -l

# If a docker-compose override changed, validate the merged result too
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config -q
```

If you changed the installers:

```bash
bash -n install.sh                  # bash syntax
python3 -m py_compile install.py    # python syntax
./install.sh --dry-run              # preview; must make NO changes
```

Never "test" an installer by actually running it — use `--dry-run`. Keep
`install.sh`, `install.py`, `README.md`, and `INSTALL.md` in sync in the same
change.
