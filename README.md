# Hermes Agent + Windmill + Hindsight

A self-hosted stack pairing the [Hermes Agent](https://hermes-agent.nousresearch.com)
gateway with [Windmill](https://www.windmill.dev) and
[Hindsight](https://hindsight.vectorize.io) memory, behind a single Caddy ingress,
with the Windmill side pre-wired to call Hermes as an OpenAI-compatible endpoint.

## What's in here

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The full stack: Hermes, Hindsight, Windmill (db/server/workers/LSP), Caddy |
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

```sh
cp .env.example .env        # then edit it (see below)
make bootstrap              # init → apikey → wizard → secure → pull → up → health
```

`make bootstrap` will:

1. create `.env` and the data directories,
2. generate `API_SERVER_KEY`,
3. run the interactive Hermes setup wizard (writes `~/.hermes/.env`),
4. `chmod 600` the secret files,
5. pull images, start the stack, and probe health.

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
    ├── fact extraction (via LM Studio)                   (PostgreSQL 16 + pgvector)
    └── knowledge graph
```

### LM Studio setup

Hindsight uses your local LM Studio instance for memory extraction and synthesis.
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

The `hindsight-hermes` package registers itself automatically via Python entry
points — no manual config file edits needed.

**From inside the Hermes container:**

```powershell
docker exec hermes uv pip install hindsight-hermes
docker restart hermes
```

**Run the setup wizard** (once Hermes has restarted):

```
hermes plugins setup hindsight
```

When prompted for the Hindsight base URL, use the **internal Docker hostname**:

```
http://hindsight:8888
```

> ⚠️ Do not use `http://hindsight.localhost` here — that hostname only resolves
> on your host machine via Caddy. From inside the Hermes container, the correct
> address is `http://hindsight:8888` (Docker service name on the `memory` network).

The three URLs and where each is used:

| URL | Used from |
|---|---|
| `http://hindsight:8888` | Inside containers — Hermes plugin, MCP config |
| `http://localhost:8888` | Host machine — PowerShell API testing |
| `http://hindsight.localhost` | Host machine browser — via Caddy UI |

### Testing memory

**1. Check Hindsight is healthy:**
```powershell
Invoke-RestMethod -Uri "http://localhost:8888/health"
```

**2. Open the memory browser:**
```
http://localhost:9999
```

**3. Test retain/recall from Hermes** (in a Hermes chat session):
```
Remember this: I work at Thoughtworks in Melbourne as a Lead Consultant.
```

Then in a **new session** (clears working memory):
```
What do you know about where I work?
```

Hindsight should surface the retained fact via `hindsight_recall`.

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
gives faster, GPU-accelerated local inference than the CPU-only `ollama` container —
but it can't run *in* `ollama` or any other container, since Docker Desktop on macOS
doesn't pass the GPU through to containers. It runs as a native host process instead,
reachable from the stack via `host.docker.internal` (same pattern as the LM Studio
backend below).

See [`mlx/README.md`](mlx/README.md) for setup, model sizing for 8/16/32 GB Macs, and
how to point Hindsight and/or Hermes at it. Quick version:

```sh
pip install mlx-lm
./mlx/serve.sh                 # serves an OpenAI-compatible API on :8080
make mlx                       # routes Hermes's own model calls through it
```

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
├── wmill.yaml                          # sync config (edit baseUrl)
├── hermes_endpoint.resource-type.yaml  # resource type: { base_url, api_key }
└── f/hermes/
    ├── api_key.variable.yaml           # secret variable (placeholder)
    ├── local.resource.yaml             # a hermes_endpoint resource → $var:f/hermes/api_key
    ├── client.py                       # shared module: get_client(), chat(), main()=model list
    └── chat.py                         # example consumer: from f.hermes.client import chat
```

### Push it

This assumes you've installed the wmill cli (`npm install -g windmill-cli`):

```sh
cd windmill
wmill workspace add main main http://windmill.localhost   # one-time
# set the real key into the secret before pushing:
#   edit f/hermes/api_key.variable.yaml  (value: <your API_SERVER_KEY>)
wmill generate-metadata          # creates .script.yaml + lockfiles
wmill sync push
```

Prefer the UI? Create a resource type `hermes_endpoint` with two string fields
(`base_url`, `api_key`), a secret variable `f/hermes/api_key`, and a resource
`f/hermes/local` of that type — then paste `client.py` / `chat.py` into new scripts.

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

## Make targets

| Target | Does |
|---|---|
| `make bootstrap` | Full first-run sequence |
| `make up` / `down` / `restart` | Lifecycle |
| `make logs` / `ps` | Follow logs / status |
| `make health` | Probe Hermes `/health` and Windmill `/api/version` |
| `make apikey` | Generate `API_SERVER_KEY` into `.env` if empty |
| `make backup` | `pg_dump` of Windmill + Hindsight Postgres, tar of Hermes `/opt/data` → `./backups/` |
| `make pull` | Pull latest images |

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
- **Hindsight can't reach LM Studio** — confirm `host.docker.internal` resolves
  from inside the container: `docker exec hindsight curl http://host.docker.internal:1234/v1/models`
- **Hermes plugin can't reach Hindsight** — use `http://hindsight:8888` not
  `http://hindsight.localhost` (see [Installing the Hermes plugin](#installing-the-hermes-plugin)).
- **Hermes errors after `make headroom`** — check Headroom is running:
  `docker compose ps headroom` and `docker compose logs headroom`. Ensure
  `OPENROUTER_API_KEY` is set in `.env`. Revert with `make headroom-revert`.
- **`savings_percent` stays 0** — expected for the first few requests while
  Headroom calibrates. Check `/v1/compress` with a test payload to confirm
  compression is active.

## CI

`.github/workflows/ci.yml` runs on every push/PR:

- **compose** — `docker compose config` (validates structure + `${VAR}` expansion against `.env.example`) and `caddy validate` on the `Caddyfile`.
- **python** — `ruff check`, `py_compile`, and a YAML parse pass over `windmill/`.

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
