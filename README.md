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

## Architecture

Four Docker networks keep traffic segmented:

- **`backend`** — Postgres ⇄ Windmill server/workers. The DB is reachable by nothing else.
- **`edge`** — Caddy ⇄ Windmill server, Windmill LSP, Hermes (everything it proxies).
- **`agent`** — Windmill server/workers ⇄ the Hermes gateway API (`hermes:8642`).
- **`memory`** — Hindsight ⇄ Hermes. Isolated from the edge and backend networks.

The Hermes dashboard and the OpenAI-compatible API run **inside the single Hermes
container** (the dashboard is a supervised s6 service — it cannot run as a
separate container).

## Prerequisites

- Docker Engine + Compose v2
- `make`, `openssl`, `curl`
- [LM Studio](https://lmstudio.ai) running locally with a chat model and an
  embedding model both loaded and served (see [LM Studio setup](#lm-studio-setup))
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
multi-strategy retrieval (semantic, keyword, temporal, graph) in a single
container with embedded PostgreSQL — no separate vector store needed.

### How it fits

```
Hermes Agent
└── hindsight plugin (hindsight_retain / hindsight_recall / hindsight_reflect)
        ↓ http://hindsight:8888
    Hindsight container
    ├── embedded PostgreSQL + pgvector
    ├── fact extraction (via LM Studio)
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
| `HINDSIGHT_DATA_DIR` | `C:/Containers/hindsight` | Persists embedded PostgreSQL data across restarts |
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

`.localhost` resolves to `127.0.0.1` on most systems. From other machines on your
LAN, either add host entries or switch to a real domain (see below).

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
| `make backup` | `pg_dump` of Windmill + tar of Hermes `/opt/data` → `./backups/` |
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
