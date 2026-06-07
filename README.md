# Hermes Agent + Windmill

A self-hosted stack pairing the [Hermes Agent](https://hermes-agent.nousresearch.com)
gateway with [Windmill](https://www.windmill.dev), behind a single Caddy ingress,
with the Windmill side pre-wired to call Hermes as an OpenAI-compatible endpoint.

## What's in here

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The full stack: Hermes, Windmill (db/server/workers/LSP), Caddy |
| `.env.example` | Every configurable knob — copy to `.env` |
| `Caddyfile` | Reverse-proxy routing for Windmill + the Hermes dashboard/API |
| `Makefile` | `bootstrap`, lifecycle, health, backups, key generation |
| `windmill/` | wmill-syncable resource type, resource, secret, and example scripts |

## Architecture

Three Docker networks keep traffic segmented:

- **`backend`** — Postgres ⇄ Windmill server/workers. The DB is reachable by nothing else.
- **`edge`** — Caddy ⇄ Windmill server, Windmill LSP, Hermes (everything it proxies).
- **`agent`** — Windmill server/workers ⇄ the Hermes gateway API (`hermes:8642`).

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

## Troubleshooting

- **Permission denied on `/opt/data`** — the container runs as a non-root user
  (UID 10000). Set `HERMES_UID`/`HERMES_GID` (or `PUID`/`PGID`) to your host user.
- **Hermes healthcheck unhealthy** — the `/health` path is best-effort; if your
  version returns 401/404 there, switch to the authenticated `/v1/models` probe
  (commented in `docker-compose.yml`).
- **LSP cache permission errors** — non-critical; `chown` `WM_LSP_CACHE_DIR` to
  the container UID or remove the mount.

## Sources

Hermes behaviour follows the official
[Hermes Agent Docker guide](https://hermes-agent.nousresearch.com/docs/user-guide/docker).
Windmill sync follows the [wmill CLI docs](https://www.windmill.dev/docs/advanced/cli).
