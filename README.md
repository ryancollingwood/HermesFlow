# Hermes Agent + Windmill + Hindsight

A self-hosted stack pairing the [Hermes Agent](https://hermes-agent.nousresearch.com)
gateway with [Windmill](https://www.windmill.dev) and
[Hindsight](https://hindsight.vectorize.io) memory, behind a single Caddy ingress,
with the Windmill side pre-wired to call Hermes as an OpenAI-compatible endpoint.

## What's in here

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The full stack: Hermes, Hindsight, Windmill (db/server/workers/LSP), Caddy |
| `docker-compose.gpu.yml` | Optional override adding NVIDIA GPU passthrough to Ollama (`--gpu`) |
| `install.sh` / `install.py` | Non-interactive installer — bash and pure-Python (Windows-friendly) versions ([step-by-step](INSTALL.md)) |
| `.env.example` | Every configurable knob — copy to `.env` |
| `Caddyfile` | Reverse-proxy routing for Windmill + Hermes dashboard/API + Hindsight UI |
| `Makefile` | `bootstrap`, lifecycle, health, backups, key generation |
| `windmill/` | wmill-syncable resource type, resource, secret, and example scripts |
| `mlx/` | Host-native MLX inference server for Apple Silicon (setup + launch script) |

## Architecture

Six Docker networks keep traffic segmented:

- **`backend`** — Postgres ⇄ Windmill server/workers. The DB is reachable by nothing else.
- **`edge`** — Caddy ⇄ all user-facing services (Windmill, Hermes, Hindsight, Headroom, Ollama).
- **`agent`** — Windmill server/workers ⇄ Hermes gateway (`hermes:8642`). Also carries
  Hermes → Headroom (`headroom:8787`).
- **`memory`** — Hindsight ⇄ Hermes. Isolated from edge and backend.
- **`inference`** — Ollama ⇄ Hermes, Hindsight, and Windmill workers (local LLM layer).
- **`monitoring`** — Prometheus, Alertmanager, Grafana, cAdvisor, exporters, Loki,
  Promtail, and Headroom. Isolated from application networks.

The Hermes dashboard and the OpenAI-compatible API run **inside the single Hermes
container** (the dashboard is a supervised s6 service — it cannot run as a
separate container).

## Prerequisites

- Docker Engine + Compose v2
- `make`, `openssl`, `curl`
- (Optional) the [`wmill` CLI](https://www.windmill.dev/docs/advanced/cli) for pushing the Windmill assets

## Quick start

Two paths — pick one.

### A. Scripted, no wizard (recommended for a fresh host)

```sh
OPENROUTER_API_KEY=sk-or-... ./install.sh
# or: ./install.sh --provider openrouter --api-key sk-or-... --model openai/gpt-4o-mini
```

Pick a **profile** for common scenarios (explicit flags still override it):
`--profile minimal` (gateway only), `full` (+ Headroom), `gpu` (NVIDIA host),
`mac` (Apple Silicon, RAM-friendly model), `server` (LAN), `remote` (all
inference at the cloud provider — no local Ollama, for low-powered hosts). E.g.
`./install.sh --profile gpu --api-key sk-or-...`. Add **`--dry-run`** to preview
the full plan without changing anything.

**On Windows (or any host without bash/make/openssl/curl), use the Python port**
— same flags, same steps, stdlib-only:

```powershell
$env:OPENROUTER_API_KEY="sk-or-..."; python install.py
# or: python install.py --provider openrouter --api-key sk-or-... --model openai/gpt-4o-mini
```

Both installers are equivalent and idempotent; `install.py` needs only Python 3
and Docker Desktop (no `make`). Flags: `--provider`, `--api-key`, `--model`,
`--no-pull`, `--skip-model-check`, `--no-memory`, `--no-windmill`,
messaging channels (`--telegram-bot-token`/`--telegram-allowed-users`,
`--discord-bot-token`/`--discord-allowed-users`), `--with-mlx` (Apple Silicon —
host-native MLX server), `--with-headroom` (route through the compression proxy),
`--bind-lan` (expose on `0.0.0.0`), `--gpu` (NVIDIA passthrough for Ollama),
Hindsight overrides (`--hindsight-model` /
`--hindsight-{retain,consolidation,reflect}-model` / `--hindsight-base-url` /
`--hindsight-mlx` / `--hindsight-api-key`), and `--env KEY=VALUE` to set any
other `.env` variable.
See [INSTALL.md](INSTALL.md) for the full table.

**See [INSTALL.md](INSTALL.md) for a step-by-step walkthrough** of everything the
installer does and every flag.

**Telegram channel (optional).** Pass both a bot token and the allowed user IDs to
wire up Hermes's Telegram channel — they're written to `<DATA_DIR>/.env` (the
file Hermes reads):

```sh
./install.sh --provider openrouter --api-key sk-or-... \
  --telegram-bot-token 123456789:ABCdef... \
  --telegram-allowed-users 11111111,22222222
```

`--telegram-allowed-users` is a comma-separated list of numeric Telegram user IDs
and is **required** alongside the token — the channel won't be set up with one but
not the other, so the bot is never left open to anyone who finds it. (Equivalent
to setting `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USERS` in `<DATA_DIR>/.env`.)

`install.sh` is non-interactive and idempotent. It checks prerequisites,
**validates the model against the provider's `/models` list** (failing early with
suggestions on a typo — pass `--skip-model-check` to bypass), creates `.env`,
sets `HERMES_UID`/`HERMES_GID` to your host user, generates **all** required
secrets (`make secrets`), writes the provider key to `<DATA_DIR>/.env` (the file
Hermes reads — same one the wizard produces), pulls images, starts the stack,
sets the default model, probes Hermes end-to-end, enables the **Hindsight memory
provider** (pass `--no-memory` to skip), and preps **Windmill** — creates the
`main` workspace, pre-installs the worker Python, and registers Windmill with
Hermes over MCP (pass `--no-windmill` to skip). Other providers:
`--provider anthropic|openai`; choose a model with `--model <id>`. Re-run any
time; it only fills blanks.

> Catalog presence is not a callability guarantee — a model can be **listed** by
> the provider yet rejected for your key/tier (e.g. OpenRouter returns 404 "No
> allowed providers"). The list check rules out typos; the end-to-end probe at
> the end is the real test.

### B. Interactive wizard

```sh
cp .env.example .env        # then edit it (see below)
make bootstrap              # init → secrets → wizard → secure → pull → up → health
```

`make bootstrap` will:

1. create `.env` and the data directories,
2. generate every required secret — `API_SERVER_KEY`, `WM_DB_PASSWORD`,
   `HINDSIGHT_DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` (`make secrets`),
3. run the interactive Hermes setup wizard (writes `<DATA_DIR>/.env`),
4. `chmod 600` the secret files,
5. pull images, start the stack, and probe health.

> **Secrets must be generated _before_ the first `up`.** Both paths do this. If
> you rotate `HINDSIGHT_DB_PASSWORD` or `WM_DB_PASSWORD` _after_ a database
> volume has been initialized, Postgres keeps the original password and the
> service fails auth — Postgres only applies `POSTGRES_PASSWORD` on first init.

> **Provider key path.** The `hermes` service in `docker-compose.yml` leaves
> `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` **commented out**
> and reads them from `<DATA_DIR>/.env` instead. Setting them only in the
> top-level `.env` does **not** reach Hermes — use the wizard, `install.sh`, or
> uncomment those lines in the compose `hermes` environment.

Run `make` on its own to list all targets.

---

## Hindsight memory

[Hindsight](https://hindsight.vectorize.io) is the memory layer for Hermes. It
provides structured fact extraction, entity resolution, a knowledge graph, and
multi-strategy retrieval (semantic, keyword, temporal, graph), backed by a
dedicated external PostgreSQL + pgvector container (`hindsight_db`) rather
than the embedded `pg0` instance Hindsight ships by default.

### How it fits

```
Hermes Agent
└── hindsight plugin (hindsight_retain / hindsight_recall / hindsight_reflect)
        ↓ http://hindsight:8888
    Hindsight container ── HINDSIGHT_API_DATABASE_URL ──→ hindsight_db container
    ├── fact extraction                   (PostgreSQL 16 + pgvector)
    └── knowledge graph
```

### LM Studio setup

If using LMStudio for Hindsight for memory extraction and synthesis, you'll need to make some tweaks to allow for multiple serving of models.
Before starting the stack:

1. Open LM Studio → Developer tab → start the local server
2. Load your chat model (e.g. `google/gemma-4-e4b`)
3. Enable **Multi-Model Serving** and load an embedding model
   (e.g. `text-embedding-nomic-embed-text-v1.5`)
4. Confirm both are serving:

```powershell
Invoke-RestMethod -Uri "http://localhost:1234/v1/models" | ConvertTo-Json
```

Both model IDs should appear in the response.

### Hindsight configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `HINDSIGHT_LLM_API_KEY` | `lm-studio` | Passed to Hindsight as `HINDSIGHT_API_LLM_API_KEY` |
| `HINDSIGHT_LLM_BASE_URL` | `http://host.docker.internal:1234/v1` | LM Studio endpoint (host machine) |
| `HINDSIGHT_LLM_MODEL` | `google/gemma-4-e4b` | Model ID exactly as reported by `/v1/models` |
| `HINDSIGHT_DB_DATA_DIR` | `${HOME}/HermesFlow/hindsight/db` | Persists `hindsight_db` PostgreSQL data across restarts |
| `HINDSIGHT_API_PORT` | `8888` | REST API port |
| `HINDSIGHT_UI_PORT` | `9999` | Web UI port (memory browser) |
| `HINDSIGHT_API_KEY` | _(empty)_ | Optional bearer token to protect the API endpoint |
| `HINDSIGHT_BASE_URL` | `http://hindsight:8888` | Used by Hermes plugin to reach Hindsight internally |

### Installing the Hermes plugin

> **`./install.sh` does all of this for you** (unless you pass `--no-memory`).
> The steps below are the manual equivalent, also used by the interactive
> (`make bootstrap`) path.

**No manual `pip install` is needed.** The `hindsight-client` package ships in
the hermes image and registers `hindsight` as a memory provider via Python entry
points; Hermes also auto-installs/upgrades it on session start if it's missing or
outdated. You only have to point Hermes at the provider.

The provider reaches Hindsight via the **`HINDSIGHT_API_URL`** environment
variable (current versions read `HINDSIGHT_API_URL`, _not_ `HINDSIGHT_BASE_URL`);
`docker-compose.yml` passes both into the `hermes` container, pointed at the
internal `http://hindsight:8888` (see
[Hindsight configuration](#hindsight-configuration-env)). For a local, no-auth
Hindsight no API key is required — `HINDSIGHT_API_KEY` is optional.

**Enable memory** — tell Hermes to use the provider. This sets a `memory:` block
in `/opt/data/config.yaml` (the bind-mounted Hermes data directory) via
`hermes config set`, the same mechanism used for
[Headroom routing](#one-time-setup-after-first-boot):

```sh
make memory
```

(`make memory-revert` disables it again.) Under the hood this runs:

```bash
docker exec hermes hermes config set memory.memory_enabled true
docker exec hermes hermes config set memory.provider hindsight
docker exec hermes hermes config set memory.user_profile_enabled true
docker exec hermes hermes config set memory.write_approval false
docker restart hermes
```

This produces a `memory:` key like:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  write_approval: false
  memory_char_limit: 2200
  user_char_limit: 1375
  provider: hindsight
  nudge_interval: 10
```

There is no `hermes plugins setup` wizard — `hermes config set` is the only
supported way to change these keys, and `provider: hindsight` is what actually
points Hermes's memory layer at the plugin (the env vars above only control
how the plugin, once selected, reaches the Hindsight container).

`HINDSIGHT_BASE_URL` should stay on the **internal Docker hostname**:

```
http://hindsight:8888
```

> ⚠️ Do not set `HINDSIGHT_BASE_URL` to `http://hindsight.localhost` — that
> hostname only resolves on your host machine via Caddy. From inside the
> Hermes container, the correct address is `http://hindsight:8888` (Docker
> service name on the `memory` network).

The three URLs and where each is used:

| URL | Used from |
|---|---|
| `http://hindsight:8888` | Inside containers — Hermes plugin, MCP config |
| `http://localhost:8888` | Host machine — API testing |
| `http://hindsight.localhost` | Host machine browser — via Caddy UI |

### Testing memory

**1. Confirm Hermes sees the provider as active _and_ reachable:**

```bash
docker exec hermes hermes memory status
```

Look for `Provider: hindsight`, `Plugin: installed ✓`, and `Status: available ✓`.
A `Status: not available ✗` with `Missing: HINDSIGHT_API_KEY` usually means the
plugin can't reach Hindsight — confirm `HINDSIGHT_API_URL` is reaching the
container (`docker exec hermes printenv HINDSIGHT_API_URL` → `http://hindsight:8888`).

**2. Check Hindsight is healthy:**

```bash
curl http://localhost:8888/health
```

For windows:
```powershell
Invoke-RestMethod -Uri "http://localhost:8888/health"
```

**3. Open the memory browser:**
```
http://localhost:9999
```

**4. Test retain/recall from Hermes** (in a Hermes chat session):
```
Remember this: I work at ACME in Melbourne as an Engineer.
```

Then in a **new session** (clears working memory):
```
What do you know about where I work?
```

Hindsight should surface the retained fact via `hindsight_recall`.

> **Extraction speed depends entirely on the Hindsight LLM backend.** Fact
> extraction runs the `HINDSIGHT_*_LLM_MODEL` models for every retain. On a
> **CPU-only Ollama** backend this is very slow — a single retain of a couple of
> sentences took ~16 minutes with `qwen2.5:3b` on CPU, long enough that a
> synchronous `hermes` call looks hung. For usable latency, run the models on a
> GPU, point Hindsight at a host-native [MLX server](#mlx-inference-apple-silicon)
> or [LM Studio](#lm-studio-setup) (set `HINDSIGHT_LLM_BASE_URL`), or use a
> smaller/faster model. Embeddings are always local (`BAAI/bge-small-en-v1.5`)
> and fast. `install.sh` pulls the configured Ollama models automatically when
> Hindsight points at the bundled `ollama` service.

### Managing zombie operations

Hindsight workers identify themselves by `HINDSIGHT_API_WORKER_ID`, which
**defaults to the container hostname**. Every `docker restart` or redeploy
generates a new hostname, so the new worker doesn't recognize the old
worker's in-flight tasks as its own — those tasks get permanently stuck in
`processing` ("zombies") and silently stop being processed.

**Symptom:** retain or consolidation jobs stuck for hours, queue depth not
decreasing, logs showing `[STUCK?]` warnings with `age` climbing well past
`HINDSIGHT_API_LLM_TIMEOUT`.

#### Prevention — pin a stable worker ID

Add to `.env`:

```bash
HINDSIGHT_API_WORKER_ID=hindsight-worker-1
```

With a stable ID, Hindsight's `recover_own_tasks()` correctly reclaims its
own stuck tasks on every startup — no manual intervention needed.

#### Diagnosing and clearing existing zombies

Use the bundled admin CLI rather than touching the database directly:

```bash
# See processing tasks grouped by worker. Workers with no recent activity
# and a growing age are dead.
docker exec hindsight hindsight-admin worker-status

# Release tasks from one known-dead worker (use the ID from worker-status)
docker exec hindsight hindsight-admin decommission-worker <old-worker-id>

# Or, if you don't know which worker is dead, release everything stuck
# across the whole fleet:
docker exec hindsight hindsight-admin decommission-workers --yes
```

Both reset `processing` rows back to `pending` so a live worker picks them
up on the next poll cycle.

> Run `decommission-workers --yes` once after first adding
> `HINDSIGHT_API_WORKER_ID` to clear any zombies accumulated before the fix
> was in place. After that, restarts should self-heal.

See the [Hindsight Admin CLI docs](https://hindsight.vectorize.io/developer/admin-cli#recovering-stuck-or-zombie-operations)
for full reference.

### Migrating from embedded Postgres

If you're upgrading an existing install that still uses Hindsight's embedded
`pg0` database, move the data to `hindsight_db` **before** cutting `hindsight`
over to the new `HINDSIGHT_API_DATABASE_URL`:

```sh
# 1. While `hindsight` is still running on embedded pg0, take a backup:
docker compose exec hindsight hindsight-admin backup /tmp/hindsight-pg0-backup.zip
docker compose cp hindsight:/tmp/hindsight-pg0-backup.zip ./hindsight-pg0-backup.zip

# 2. Start the new external Postgres container:
docker compose up -d hindsight_db

# 3. Recreate hindsight pointed at hindsight_db (applies HINDSIGHT_API_DATABASE_URL):
docker compose up -d hindsight

# 4. Restore the backup into the now-external-Postgres-backed instance:
docker compose cp ./hindsight-pg0-backup.zip hindsight:/tmp/hindsight-pg0-backup.zip
docker compose exec hindsight hindsight-admin restore /tmp/hindsight-pg0-backup.zip --yes

# 5. Verify:
docker compose exec hindsight hindsight-admin worker-status
curl http://localhost:8888/health
```

Keep `./hindsight-pg0-backup.zip` and the old `HINDSIGHT_DATA_DIR` bind mount
around as a rollback safety net until you've confirmed pre-existing memories
are present via the web UI / API.

---

## MLX inference (Apple Silicon)

If you're running this stack on an M-series Mac, [MLX](https://github.com/ml-explore/mlx)
gives faster, GPU-accelerated local inference `ollama` container —
but it can't run *in* `ollama` or any other container, since Docker Desktop on macOS
doesn't pass the GPU through to containers. It runs as a native host process instead,
reachable from the stack via `host.docker.internal` (same pattern as the LM Studio
backend below).

See [`mlx/README.md`](mlx/README.md) for setup, model sizing for 8/16/32 GB Macs, and
how to point Hindsight and/or Hermes at it. Quick version:

```sh
pip install mlx-lm
./mlx/serve.sh                 # manual: serves an OpenAI-compatible API on :8080
./mlx/install-launchd.sh        # or: always-on launchd agent, restarts on crash/reboot
make mlx                       # routes Hermes's own model calls through it
make hindsight-mlx             # routes Hindsight memory extraction through it
make mlx-status                # show install (path, version, model) + test the endpoint
```

`make mlx-status` reports the venv path, `mlx-lm` version, configured model, and
launchd state, then probes `/v1/models` and runs a one-shot chat completion
against the host server — a quick way to confirm MLX is up before routing
Hermes/Hindsight at it.

`make hindsight-mlx` rewrites the `HINDSIGHT_LLM_*` vars in `.env` to the MLX
endpoint (`MLX_BASE_URL` / `MLX_MODEL`) and recreates the `hindsight` container;
`make hindsight-mlx-revert` switches back to the bundled Ollama. The installers do
the same with `--hindsight-mlx` (e.g. `./install.sh --with-mlx --hindsight-mlx`).

---

## Headroom context compression

[Headroom](https://headroom-docs.vercel.app/) is an OpenAI-compatible proxy sidecar
that compresses Hermes LLM request context before forwarding to the upstream provider,
reducing token usage 40–95% with near-zero accuracy loss. It runs transparently — Hermes
is unaware of the compression layer.

### How it fits

```
Hermes → http://headroom:8787/v1/chat/completions → Headroom compresses → OpenRouter API
```

| Content type | Compressor | Typical savings |
|---|---|---|
| JSON / tool outputs | SmartCrusher | 70–90% |
| Source code | CodeCompressor | 40–70% |
| Build / test logs | LogCompressor | 80–95% |
| Search results | SearchCompressor | 60–80% |
| Plain text | Kompress | 30–50% |

### One-time setup after first boot

```sh
make headroom
```

This sets three Hermes config keys in `/opt/data/config.yaml` (the bind-mounted data
directory, so it persists across restarts and container rebuilds) and restarts the
`hermes` container so they take effect:

- `model.provider: custom` — **must not** be left as `openrouter`. Hermes's built-in
  OpenRouter client ignores `model.base_url` entirely and calls OpenRouter directly,
  silently bypassing Headroom. Only a non-built-in provider name (`custom`) makes Hermes
  honor the `base_url` override.
- `model.base_url: http://headroom:8787/v1` — routes Hermes's outbound chat requests
  through the proxy.
- `model.api_key` — copied from `OPENROUTER_API_KEY` so the `custom` provider has
  credentials to send; Headroom swaps in its own upstream key once `--backend openrouter`
  (set on the `headroom` service in `docker-compose.yml`) is configured.

`make bootstrap` will also prompt to run this step.

To revert to direct provider routing:

```sh
make headroom-revert
```

### Verifying Headroom is actually doing something

A "healthy" container and a configured `base_url` are not proof that traffic is flowing
through it — Hermes can silently bypass Headroom if `model.provider` is wrong (see above).
Confirm the whole path end-to-end:

```sh
# 1. Backend should read "openrouter", not the default "anthropic":
docker exec headroom curl -fsS http://localhost:8787/health | grep backend

# 2. Send a real request, then check it actually landed on /v1/chat/completions:
docker exec hermes hermes -z "Say PONG and nothing else"
docker exec headroom curl -fsS http://localhost:8787/stats | grep -o '"api_requests":[0-9]*'
# api_requests should increment — if it stays 0, Hermes is bypassing the proxy.

# 3. Decisive check — stop Headroom and confirm Hermes now FAILS (proves it's a real
#    dependency, not bypassed):
docker stop headroom
docker exec hermes hermes -z "Say PONG and nothing else"   # should error, not succeed
docker start headroom
```

Compression itself only kicks in above `min_tokens_to_crush` (500 tokens by default), so
short test prompts won't show savings in `requests_compressed` — that's expected, not a
sign of misconfiguration.

### Checking savings

```sh
# Via Caddy virtualhost:
open http://headroom.localhost/stats
open http://headroom.localhost/dashboard

# Direct (bypasses Caddy):
curl http://127.0.0.1:8787/stats
curl http://127.0.0.1:8787/stats-history

# Prometheus metrics (also scraped automatically by the stack):
curl http://127.0.0.1:8787/metrics
```

### Headroom configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `HEADROOM_DAILY_BUDGET` | _(empty)_ | USD daily spend cap — leave blank for unlimited |
| `HEADROOM_MEM_LIMIT` | `512M` | Container memory cap. Bump to `1G`–`2G` for LLMLingua ML compression |
| `HEADROOM_CPU_LIMIT` | `0.5` | Container CPU cap |

---

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `${HOME}/.hermes` | Mounted to Hermes `/opt/data` (config, sessions, keys) |
| `SHARED_DIR` | `${HOME}/.shared_agent_data` | Cross-app `/shared` drop folder |
| `WM_DATA_DIR` | `${HOME}/.windmill` | Postgres + Windmill logs/cache |
| `HERMES_BIND` | `127.0.0.1` | Set `0.0.0.0` for LAN access to Hermes ports |
| `HERMES_DASHBOARD` | `1` | Enable the built-in dashboard (port 9119) |
| `HERMES_DASHBOARD_INSECURE` | `1` | Drops the OAuth gate — **exposes API keys**; trusted LAN only |
| `API_SERVER_ENABLED` | `true` | OpenAI-compatible API on 8642 (needed by Windmill + dashboard) |
| `API_SERVER_KEY` | _(empty)_ | Required when enabled (≥8 chars) — `make apikey` fills it |
| `HERMES_UID` / `HERMES_GID` | `1000` | Set to your host user so the container can write the bind mount |
| `WM_DB_PASSWORD` | `windmill` | **Change this** |

> Docker Compose does **not** expand `~`. It does expand `${HOME}`. If `${HOME}`
> ever misbehaves, hardcode absolute paths.

## Accessing the services

With the default Caddy config (plain HTTP on `.localhost` hostnames):

| Service | URL |
|---|---|
| Windmill | `http://windmill.localhost` |
| Hermes dashboard | `http://hermes.localhost` |
| Hermes API | `http://hermes-api.localhost` (or `http://localhost:8642`) |
| Hindsight UI | `http://hindsight.localhost` (or `http://localhost:9999`) |
| Hindsight API | `http://localhost:8888` |
| Headroom dashboard | `http://headroom.localhost` (or `http://localhost:8787`) |
| Alertmanager | `http://alertmanager.localhost` |

`.localhost` resolves to `127.0.0.1` on most systems. From other machines on your
LAN, either add host entries or switch to a real domain (see below).

Loki has no UI of its own and no Caddy route — query container logs via Grafana's
**Explore** tab with the `Loki` datasource (provisioned automatically).

### Real domain + TLS

In `Caddyfile`, uncomment the `email` line and replace each `http://<name>.localhost`
with `<name>.yourdomain.com` (drop the `http://`). Caddy then provisions
Let's Encrypt certificates automatically. Make sure 80/443 are reachable.

## Windmill ⇄ Hermes integration

The `windmill/` folder makes Hermes a **reusable** endpoint across all your scripts
instead of hardcoding the URL/key each time.

```
windmill/
├── wmill.yaml                          # sync config (scope: f/hermes/** only)
├── SYNC.md                             # what push/pull do to your content — read before forcing a push
├── hermes_endpoint.resource-type.yaml  # resource type: { base_url, api_key }
└── f/hermes/                           # VERSIONED Hermes code/config (this is the only synced folder)
    ├── folder.meta.yaml                # folder permissions/owners (tracked so a push won't strip them)
    ├── api_key.variable.yaml           # secret variable (placeholder)
    ├── local.resource.yaml             # a hermes_endpoint resource → $var:f/hermes/api_key
    ├── client.py                       # shared module: get_client(), chat(), main()=model list
    └── chat.py                         # example consumer: from f.hermes.client import chat
```

> **Two folders, two purposes.** `f/hermes/` is **versioned code/config** and is
> the *only* folder sync touches. Non-secret **runtime state** (last-run
> timestamps, cursors, …) belongs in **`f/hermes_state/`** — a folder the
> installer creates but which is **deliberately outside sync scope**, so a mirror
> push never deletes it and it never lands in git. Everything else (`u/*`, other
> `f/*`, inherited resource-types, secret variables) is ignored by sync too.
> [**windmill/SYNC.md**](windmill/SYNC.md) has the full per-scenario breakdown.

### Push it

**The installer does this for you** when the `wmill` CLI is present. `install.sh`
/ `install.py` create the `main` workspace, then run `wmill sync push` and seed
the `f/hermes/api_key` secret from your `API_SERVER_KEY`. If the CLI isn't
installed it prints how to do it later — nothing else in the install depends on
node/npm. Once the CLI is installed you can (re)push any time:

```sh
make windmill-push       # registers the CLI profile, pushes assets, seeds the api_key secret
```

> **`sync push` is a mirror, not an upload.** It makes the remote workspace match
> `windmill/`, which means it **deletes or archives any non-secret server item
> that isn't tracked here** (secret variables are untouched thanks to
> `skipSecrets`). To avoid clobbering work authored in the UI, the installer and
> `make windmill-push` **dry-run first and abort if the push would remove
> anything**, printing what it would delete. Reconcile with `make windmill-pull`,
> or override deliberately: `make windmill-push FORCE=1` (or `WMILL_FORCE_PUSH=1`
> for the installer). Use `make windmill-check` any time to see drift first.

To push by hand instead, install the wmill cli (`npm install -g windmill-cli`).
**First, make sure a `main` workspace exists on the server.** A fresh Windmill CE
has none, and `wmill workspace add` only registers the workspace *locally in the
CLI* — it does not create it server-side, so the first `wmill sync push` fails
without it. `install.sh` / `make windmill-push` create it for you; to do it by
hand, open `http://windmill.localhost`, log in (default superadmin
`admin@windmill.dev` / `changeme`), and create a workspace with id `main`.

```sh
cd windmill
wmill workspace add main main http://windmill.localhost   # one-time (registers the CLI profile)
wmill generate-metadata          # creates .script.yaml + lockfiles
wmill sync push                  # pushes the resource type, resource, and scripts
```

**Don't put the API key in the YAML.** `wmill.yaml` keeps `skipSecrets: true`, so
the secret variable is intentionally **not** pushed (and you should never write a
real key into the tracked `api_key.variable.yaml`). Set its value server-side
instead — in the UI (Variables → `f/hermes/api_key` → set value to your
`API_SERVER_KEY`), or via the API:

```sh
TOKEN=$(curl -s -H 'Content-Type: application/json' http://windmill.localhost/api/auth/login \
  -d '{"email":"admin@windmill.dev","password":"changeme"}')
curl -s -X POST http://windmill.localhost/api/w/main/variables/create \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"path":"f/hermes/api_key","value":"<your API_SERVER_KEY>","is_secret":true}'
```

Prefer the UI entirely? Create a resource type `hermes_endpoint` with two string
fields (`base_url`, `api_key`), the secret variable `f/hermes/api_key`, and a
resource `f/hermes/local` of that type — then paste `client.py` / `chat.py` into
new scripts.

### Use it in any script

```python
from f.hermes.client import hermes_endpoint, chat

def main(hermes: hermes_endpoint, prompt: str) -> str:
    return chat(hermes, prompt, model="hermes")
```

Pick the `f/hermes/local` resource for the `hermes` parameter. Set `model` to a
name the gateway actually serves — run `client.py` (or
`curl http://hermes:8642/v1/models` from a worker) to list them.

> Inside a worker, reach Hermes at `hermes:8642`, **not** `localhost` — localhost
> is the worker itself. This works because the workers share the `agent` network.

### Pull it back (version-control what's on the server)

`push` is one-way: repo → server. When you build or edit scripts/flows/resources
**in the Windmill UI**, pull them back into this repo so they're tracked in git:

```sh
make windmill-pull       # server → windmill/  (then review and commit)
git -C windmill status   # see what changed
git add windmill/ && git commit -m "windmill: sync from server"
```

What gets written is governed by [`windmill/wmill.yaml`](windmill/wmill.yaml):
`includes: ["f/**", "*.resource-type.yaml"]` scopes the pull to the `f/`
namespace (so personal `u/<you>/…` drafts stay out of the repo), and
`skipSecrets: true` writes a **placeholder** for secret variables instead of the
real value — so `git diff` never leaks `f/hermes/api_key`. Widen `includes` if you
want flows or other folders tracked too. The round-trip is just
`make windmill-pull` (author in the UI) ↔ `make windmill-push` (author in the repo).

To see whether the live server has drifted from what's committed — without
writing anything — run `make windmill-check`. It requires a clean `windmill/`
tree, pulls into it, diffs against git, prints any drift, then reverts. Exit code
is non-zero on drift, so it doubles as a pre-push or scheduled guard.

### Hermes ⇄ Windmill over MCP

That covers Windmill calling Hermes. The reverse also works: **Windmill ships an
MCP server, and Hermes is an MCP client**, so Windmill's scripts/flows *and* its
management API become callable tools inside Hermes chat sessions. `install.sh`
wires this up by default (skip with `--no-windmill`); the manual steps are below.

Hermes reaches Windmill directly over the shared `agent`/`edge` networks at
`windmill_server:8000` — no Caddy involved.

```sh
# 1. Mint a Windmill token WITH the mcp scope (a plain token can't list tools):
TOKEN=$(curl -s http://windmill.localhost/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@windmill.dev","password":"changeme"}')
MCP=$(curl -s -X POST http://windmill.localhost/api/users/tokens/create \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"label":"hermes-mcp","scopes":["mcp:all"]}')

# 2. Register it with Hermes (answer 'y' to auth, then paste the token):
docker exec -it hermes hermes mcp add windmill \
  --url http://windmill_server:8000/api/mcp/w/main/sse --auth header
#   …then enter $MCP at the "API key / Bearer token" prompt.

# 3. Verify:
docker exec hermes hermes mcp test windmill     # lists the available tools
```

With `mcp:all` Hermes sees your workspace scripts/flows (e.g. `s-f_hermes_chat`,
`s-f_hermes_client`) **plus** Windmill's admin tools (createSchedule,
listWorkers, createVariable, …). Start a **new** Hermes session for the tools to
become active.

Gotchas (all enforced by Windmill):

- The endpoint is **Streamable-HTTP `POST`**, despite the `/sse` in the path — a
  `GET` returns `405`.
- The token **must** have the `mcp:all` scope. Without it you get
  `Unauthorized: missing mcp scope` on `tools/list`.
- Auth is a **Bearer header** (or a `?token=` query param) — don't send both, an
  empty bearer alongside a URL token yields `401`.
- Hermes rewrites `config.yaml` on every command and **disables** a server that
  fails to connect, so hand-editing the YAML won't stick — let `hermes mcp add`
  record the auth.

## Make targets

| Target | Does |
|---|---|
| `make bootstrap` | Full first-run sequence |
| `make up` / `down` / `restart` | Lifecycle |
| `make logs` / `ps` | Follow logs / status |
| `make health` | Probe Hermes `/health` and Windmill `/api/version` |
| `make apikey` | Generate `API_SERVER_KEY` into `.env` if empty |
| `make secrets` | Generate every required secret (`API_SERVER_KEY`, `WM_DB_PASSWORD`, `HINDSIGHT_DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`) that's blank or a weak default |
| `make backup` | `pg_dump` of Windmill + Hindsight Postgres, tar of Hermes `/opt/data` → `./backups/` |
| `make pull` | Pull latest images |
| `make hermes-pkg PKG=...` | Install a pure-Python package for Hermes tools (e.g. `firecrawl-py`) into the writable extras dir — see [Troubleshooting](#troubleshooting) |

## Security notes

- `HERMES_DASHBOARD_INSECURE=1` serves the dashboard's API surface (model keys,
  session data) to anyone who can reach port 9119. Only run it on a LAN you
  control, or front it with auth at Caddy.
- The Hermes API server has its own `API_SERVER_KEY` — keep it secret and rotate it.
- `.env`, `backups/`, and `data/` are git-ignored. Never commit real secrets;
  the `wmill.yaml` keeps `skipSecrets: true` so pulls don't write secret values.
- `HINDSIGHT_API_KEY` is optional but recommended if Hindsight is exposed beyond
  localhost — set it and Hermes will forward it automatically via `HINDSIGHT_BASE_URL`.

## Troubleshooting

- **Permission denied on `/opt/data`** — the container runs as a non-root user
  (UID 10000). Set `HERMES_UID`/`HERMES_GID` (or `PUID`/`PGID`) to your host user.
- **Hermes healthcheck unhealthy** — the `/health` path is best-effort; if your
  version returns 401/404 there, switch to the authenticated `/v1/models` probe
  (commented in `docker-compose.yml`).
- **LSP cache permission errors** — non-critical; `chown` `WM_LSP_CACHE_DIR` to
  the container UID or remove the mount.
- **Windmill Python scripts fail to deploy with `Couldn't locate the interpreter`**
  — `uv` downloads a managed CPython into the shared worker cache on first use;
  a worker killed mid-extraction (or multiple replicas racing on a brand-new
  interpreter) can leave a corrupt half-install that uv won't repair, so *every*
  Python script then fails. The `windmill_cache_init` service in
  `docker-compose.yml` runs before every `docker compose up` / restart and
  self-heals this: it checks each cached interpreter actually runs, deletes any
  that don't, and reinstalls 3.12 if missing — `windmill_worker` won't start
  until it finishes. If you hit the error anyway (e.g. corruption happened
  between healer runs), force a heal manually:
  ```sh
  docker compose up windmill_cache_init
  ```
  A cached deployment error sticks to the script even after the interpreter is
  fixed — delete and re-push it (or save a new version) to force a clean
  re-lock once the interpreter is in place.
- **Windmill `wmill sync push` fails / workspace not found** — a fresh Windmill
  CE has no `main` workspace and `wmill workspace add` doesn't create one
  server-side. Create it in the UI (or let `install.sh` do it). See
  [Push it](#push-it).
- **Hindsight can't reach LM Studio** — confirm `host.docker.internal` resolves
  from inside the container: `docker exec hindsight curl http://host.docker.internal:1234/v1/models`
- **Hermes plugin can't reach Hindsight** — use `http://hindsight:8888` not
  `http://hindsight.localhost` (see [Installing the Hermes plugin](#installing-the-hermes-plugin)).
- **Hermes returns HTTP 404 "No allowed providers" / "No endpoints found"** —
  the request authenticated fine (not a 401), but the configured model id isn't
  served by your provider. The image seeds a default that may not exist on
  OpenRouter; point it at a real model:
  `docker exec hermes hermes config set model.default openai/gpt-4o-mini`
  (`install.sh` does this for you).
- **Hermes errors after `make headroom`** — check Headroom is running:
  `docker compose ps headroom` and `docker compose logs headroom`. Ensure
  `OPENROUTER_API_KEY` is set in `.env`. Revert with `make headroom-revert`.
- **`savings_percent` stays 0** — expected for the first few requests while
  Headroom calibrates. Check `/v1/compress` with a test payload to confirm
  compression is active.
- **Hermes tool needs a package that isn't in the image (e.g. `firecrawl-py`
  for `web.backend: firecrawl`) and lazy install fails with "Permission
  denied"** — this is intentional, two-layered hardening: the image sets
  `HERMES_DISABLE_LAZY_INSTALLS=1` and ships `/opt/hermes` (including the
  venv) read-only, so a prompt-injected `pip install` can't land malicious
  code in the gateway. Don't unset the env var or `chown` the venv to work
  around this — that disables the protection for every package, not just the
  one you need. Instead install the package into the writable `/opt/data`
  bind mount, which is already on `PYTHONPATH` (`docker-compose.yml`):
  `make hermes-pkg PKG=firecrawl-py==4.17.0`. This only works for pure-Python
  packages (no compiled extensions, no console-script entry points), since
  it's a `--target` install outside the venv's own package metadata — fine
  for `firecrawl-py`.

## CI

`.github/workflows/ci.yml` runs on every push/PR:

- **compose** — `docker compose config` (validates structure + `${VAR}` expansion against `.env.example`) and `caddy validate` on the `Caddyfile`.
- **python** — `ruff check`, `py_compile`, and a YAML parse pass over `windmill/`.
- **windmill-consistency** — checks that every tracked script has its generated `*.script.yaml` and that every referenced inline lockfile exists, so assets edited in the UI and pulled without their metadata can't slip into the repo. This is a server-free guard; for a true repo-vs-live-server diff run `make windmill-check`.

Run the same checks locally before pushing:

```sh
make ci          # = make validate + make lint
```

## Sources

Hermes behaviour follows the official
[Hermes Agent Docker guide](https://hermes-agent.nousresearch.com/docs/user-guide/docker).
Windmill sync follows the [wmill CLI docs](https://www.windmill.dev/docs/advanced/cli).
Hindsight setup follows the [Hindsight docs](https://hindsight.vectorize.io) and the
[hindsight-hermes plugin README](https://github.com/NousResearch/hermes-agent/blob/main/plugins/memory/hindsight/README.md).
