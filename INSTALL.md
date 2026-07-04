# Installer walkthrough (`install.sh` / `install.py`)

This document explains exactly what the non-interactive installers do, step by
step. There are two equivalent implementations:

- **`install.sh`** — bash; uses `make`, `openssl`, `curl`.
- **`install.py`** — pure Python standard library; no `make`/`bash`/`openssl`/`curl`.
  Use this on Windows or any host without those tools.

Both are **idempotent**: re-running only fills blanks and never clobbers existing
secrets, so it's safe to run again after editing `.env` or to repair a partial
install.

```sh
# bash (macOS / Linux)
OPENROUTER_API_KEY=sk-or-... ./install.sh

# python (Windows / anywhere)
python install.py --provider openrouter --api-key sk-or-...
```

## Examples

Install on Mac

```bash
OPENROUTER_API_KEY=sk-or-v1-******************** python3 install.py --profile mac --hindsight-model qwen2.5:3b --model deepseek/deepseek-v4-flash --with-headroom --telegram-bot-token ****************** --telegram-allowed-users ************
```

## Profiles

`--profile <name>` applies a preset bundle of flags for a common scenario. Any
explicit flag you also pass **overrides** the profile, so you can start from a
preset and tweak (e.g. `--profile gpu --bind-lan`).

| Profile | Bundles | For |
|---|---|---|
| `minimal` | `--no-memory --no-windmill` | Just the Hermes gateway + provider. |
| `full` | `--with-headroom --with-ollama` (memory + windmill are on by default) | Everything, plus context compression and local inference. |
| `gpu` | `--gpu` (implies `--with-ollama`) | Linux/WSL2 NVIDIA host — in-container Ollama with the GPU and full-size models. |
| `mac` | `--hindsight-model qwen2.5:3b --with-ollama` | Apple Silicon — one small RAM-friendly model (avoids the two-model reload thrash on the CPU-only container). |
| `server` | `--bind-lan` | Expose on the LAN (auto-generates a Hindsight API key). |
| `remote` | routes Hindsight's LLM at the cloud provider (no local Ollama models pulled) | Low-powered hosts — all inference is remote. Needs an OpenAI-compatible provider (`openrouter`/`openai`). |

```sh
./install.sh --profile minimal
python install.py --profile gpu --api-key sk-or-...
./install.sh --profile remote --api-key sk-or-...   # all inference remote
```

### Dry run

`--dry-run` prints the fully-resolved plan (provider, model, profile, every
toggle, the secrets it would generate, and the ordered steps) and exits **without
writing `.env`, creating directories, or touching any container**. Use it to
preview exactly what an install would do:

```sh
./install.sh --profile remote --api-key sk-or-... --dry-run
```

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--provider {openrouter,anthropic,openai}` | `openrouter` | Which AI provider Hermes calls. Picks the key var, a default model, and the `/models` endpoint. |
| `--api-key <key>` | from `$OPENROUTER_API_KEY` etc. | Provider API key. Falls back to the matching environment variable. |
| `--model <id>` | provider default (`openai/gpt-4o-mini` for OpenRouter) | Model Hermes defaults to. |
| `--no-pull` | off | Skip `docker compose pull` (use already-pulled images). |
| `--no-build` | off | Skip building the local Hermes image (use only if it's already built). |
| `--skip-model-check` | off | Don't validate the model against the provider's `/models` list. |
| `--no-memory` | off | Don't enable the Hindsight memory provider. |
| `--no-windmill` | off | Don't pre-install the Windmill worker Python, create the workspace, or wire up MCP. |
| `--telegram-bot-token <token>` | — | BotFather token to enable the Telegram channel. **Requires** `--telegram-allowed-users`. |
| `--telegram-allowed-users <id,id,…>` | — | Comma-separated numeric Telegram user IDs allowed to use the bot. **Requires** `--telegram-bot-token`. |
| `--with-mlx` | off | Install the host-native MLX inference server (Apple Silicon macOS only). |
| `--hindsight-model <id>` | from `.env` | Set **every** Hindsight LLM scope (main/retain/consolidation/reflect) to this model. |
| `--hindsight-retain-model <id>` | — | Override just the retain scope. |
| `--hindsight-consolidation-model <id>` | — | Override just the consolidation scope. |
| `--hindsight-reflect-model <id>` | — | Override just the reflect scope. |
| `--hindsight-base-url <url>` | from `.env` | Hindsight LLM endpoint (Ollama / LM Studio / MLX). |
| `--hindsight-mlx` | off | Point Hindsight's extraction LLM at the host MLX server (`MLX_BASE_URL`/`MLX_MODEL`). Pairs with `--with-mlx`. |
| `--hindsight-api-key <key>` | — | Bearer token protecting the Hindsight API (auto-generated if `--bind-lan` and unset). |
| `--discord-bot-token <token>` | — | Discord bot token. **Requires** `--discord-allowed-users`. |
| `--discord-allowed-users <id,id,…>` | — | Comma-separated Discord user IDs allowed to use the bot. **Requires** `--discord-bot-token`. |
| `--with-headroom` | off | Route Hermes through the Headroom context-compression proxy (OpenRouter only). |
| `--with-baserow` | off | Add Baserow (structured-data UI + REST API + MCP) by layering [`docker-compose.baserow.yml`](docker-compose.baserow.yml) on via `COMPOSE_FILE`. AI fields default to local Ollama. |
| `--with-directus` | off | Add Directus (triage UI + REST/GraphQL API + MCP) by layering [`docker-compose.directus.yml`](docker-compose.directus.yml) on via `COMPOSE_FILE`. |
| `--with-observability` | off | Add Prometheus, Grafana, exporters, and Loki/Promtail by layering [`docker-compose.observability.yml`](docker-compose.observability.yml) on via `COMPOSE_FILE`. |
| `--with-ollama` | off (defaults to `http://host.docker.internal:11434`) | Add a local Ollama container by layering [`docker-compose.ollama.yml`](docker-compose.ollama.yml) on via `COMPOSE_FILE`, and repoint `HINDSIGHT_LLM_BASE_URL`/`BASEROW_OLLAMA_HOST` at it. Without this flag, both vars already default to an Ollama on the Docker host. |
| `--external-ollama <url>` | — | Point `HINDSIGHT_LLM_BASE_URL`/`BASEROW_OLLAMA_HOST` at an Ollama at a URL other than the Docker-host default (a different LAN box, a non-standard port), instead of starting the bundled container. Mutually exclusive with `--with-ollama`. |
| `--bind-lan` | off | Expose Hermes/Hindsight/Ollama on `0.0.0.0` instead of loopback. |
| `--gpu` | off | NVIDIA GPU passthrough for the Ollama container (Linux/WSL2 + nvidia-container-toolkit). Implies `--with-ollama`. No-op on macOS. |
| `--env KEY=VALUE` | — | Set any other `.env` variable. Repeatable. |
| `--profile <name>` | — | Apply a preset bundle (see [Profiles](#profiles)). |
| `--dry-run` | off | Print the resolved plan and exit without changing anything. |

## Steps

### 1. Check prerequisites
Verifies `docker` and `docker compose` v2 are present (bash also checks
`make`/`openssl`/`curl`). Aborts early if anything is missing.

### 2. Validate the model
Fetches the provider's `/models` list and checks `--model` is in it. On a typo it
**fails fast** with near-match suggestions, before pulling images or touching the
stack. Skipped with `--skip-model-check` or when no API key is given.

> Catalog presence is not a callability guarantee — a model can be *listed* by
> the provider yet rejected for your key/tier. The end-to-end probe in step 9 is
> the real test.

### 3. Create `.env`
Copies `.env.example` to `.env` if it doesn't already exist; otherwise leaves the
existing `.env` untouched.

### 4. Set `HERMES_UID` / `HERMES_GID`
On macOS/Linux, sets these to the host user so the container can write the
bind-mounted data dirs. **Skipped on Windows** (Docker Desktop maps a virtual
UID).

### 5. Generate secrets
Generates every required secret that's blank or still a known-weak default:
`API_SERVER_KEY`, `WM_DB_PASSWORD`, `HINDSIGHT_DB_PASSWORD`, and
`GRAFANA_ADMIN_PASSWORD` (otherwise Grafana boots as `admin`/`changeme`).
Existing custom values are left alone.

This step also applies the `--with-ollama`, `--external-ollama`, `--gpu`,
`--with-baserow`, `--with-directus`,
`--with-observability`, `--bind-lan`,
`--hindsight-api-key`, and `--env` overrides to `.env` first, so they're in place
before the stack starts. Without either Ollama flag, `HINDSIGHT_LLM_BASE_URL` /
`BASEROW_OLLAMA_HOST` already default (from `.env.example`) to
`http://host.docker.internal:11434[/v1]` — i.e. an Ollama already running on
the Docker host. `--with-ollama` layers
[`docker-compose.ollama.yml`](docker-compose.ollama.yml) on via `COMPOSE_FILE`
(Ollama used to be part of the base stack; it's now opt-in like Baserow/Directus)
and repoints those two vars at the bundled container instead (`http://ollama:11434[/v1]`).
`--gpu` implies `--with-ollama` and additionally layers
[`docker-compose.gpu.yml`](docker-compose.gpu.yml) on (adding the NVIDIA device
reservation) and sets `CUDA_VISIBLE_DEVICES` / `OLLAMA_NUM_GPU`. On a GPU host
you'd keep Ollama in-container — there's no need for host-native MLX.
`--external-ollama <url>` skips the bundled container and instead points
`HINDSIGHT_LLM_BASE_URL` / `BASEROW_OLLAMA_HOST` at a URL other than the
Docker-host default above; it's mutually exclusive with `--with-ollama`.
`--with-baserow` layers
[`docker-compose.baserow.yml`](docker-compose.baserow.yml) on the same way
(additively, so it coexists with `--gpu`); secrets `BASEROW_SECRET_KEY` /
`BASEROW_DB_PASSWORD` / `BASEROW_REDIS_PASSWORD` are generated by the secrets step.

> Secrets must be generated **before** the first `up`. Postgres only applies
> `POSTGRES_PASSWORD` on first init, so rotating a DB password after the volume
> exists breaks auth.

### 6. Create data directories
Creates the bind-mount directories (`DATA_DIR`, `SHARED_DIR`, the Windmill and
Caddy dirs). On POSIX it also `chown`s them to `HERMES_UID:HERMES_GID` via a
throwaway `alpine` container; this is skipped on Windows.

### 7. Write the provider key (and Telegram config)
Writes the provider key to `<DATA_DIR>/.env` — the file Hermes actually reads.
The `hermes` service in `docker-compose.yml` leaves provider keys **commented
out** and reads them from this file (what the interactive wizard would write), so
setting the key only in the top-level `.env` would **not** reach Hermes.

If Telegram or Discord flags were passed, `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_ALLOWED_USERS` (and/or `DISCORD_BOT_TOKEN` / `DISCORD_ALLOWED_USERS`)
are written to the same file. Each channel requires both its token **and** its
allow-list together — a channel is never configured with a token but no
allow-list, so the bot is never left open. On a re-run with the stack already up,
Hermes is restarted to pick up the new channel.

### 8. Pull images, build the Hermes image, and start the stack
`docker compose pull --ignore-buildable` (unless `--no-pull`), then
`docker compose build hermes` (unless `--no-build`) to bake the extra Python
packages from `hermes/requirements.txt` into the image, then `docker compose up -d`.
It then runs `make hermes-heal` — idempotent cleanup that removes any stray
agent-installed package overlay + `PYTHONPATH` drift so the baked venv stays
authoritative — and waits for the `hermes` container to pass its healthcheck. See
[docs/hermes-docker-build.md](docs/hermes-docker-build.md) for what the build does and why.

### 9. Set the default model and probe
Sets `model.default` (the image seeds an invalid default on OpenRouter), then
sends a one-shot `hermes -z "Say PONG"` and checks the reply — the real
end-to-end verification that the provider, key, and model all work.

### 10. Hindsight memory (skip with `--no-memory`)
- Pulls the `HINDSIGHT_*_LLM_MODEL` models (embeddings are local, so no
  embedding model is needed): via `docker exec` into the bundled `ollama`
  container when it's running, or over HTTP against `--external-ollama` when
  that's set — otherwise this step is skipped. The models come
  from `.env`, which the `--hindsight-model` / `--hindsight-*-model` /
  `--hindsight-base-url` flags above write **before** the stack starts — so e.g.
  `--hindsight-model qwen2.5:3b` makes every scope use one small model (handy on
  RAM-limited Macs, avoiding the two-model reload thrash).
- Sets `memory.provider = hindsight` (+ related keys) and restarts Hermes. No
  `pip install` is needed — the `hindsight-client` package ships in the image.
- Verifies with `hermes memory status` (`available ✓`) and checks the Hindsight
  API health endpoint.

### 11. Windmill prep (skip with `--no-windmill`)
- The shared worker Python cache is self-healing: the `windmill_cache_init`
  service in `docker-compose.yml` validates every cached interpreter and
  repairs or reinstalls it before any worker replica starts, on every
  `docker compose up` — not just at install time.
- **Creates the `main` workspace** (a fresh Windmill CE has none; `wmill
  workspace add` only registers it locally).
- **Registers Windmill with Hermes over MCP** — mints an `mcp:all`-scoped token
  and adds it via `hermes mcp add`, so Windmill scripts/flows and its management
  API become callable tools in Hermes sessions.

### 12. MLX host server (opt-in with `--with-mlx`, Apple Silicon only)
Only runs when `--with-mlx` is passed, and only on Apple Silicon macOS (it warns
and skips elsewhere). MLX must run on the **host** — Docker Desktop on macOS
doesn't pass the GPU into containers, so a containerised runtime gets no
acceleration. This step:

- creates a Python venv (`~/.mlx-venv`, override with `MLX_VENV_DIR`) and installs
  `mlx-lm` into it,
- runs [`mlx/install-launchd.sh`](mlx/install-launchd.sh) to register an always-on
  launchd agent serving an OpenAI-compatible API on `:8080` (the model downloads
  on first request; override with `MLX_MODEL` / `MLX_HOST_PORT`).

It does **not** re-route anything automatically. To use MLX afterwards, either
route Hermes through it (`make mlx`) or point Hindsight at it (set
`HINDSIGHT_LLM_BASE_URL=${MLX_BASE_URL}` and restart `hindsight`). See
[`docs/mlx.md`](docs/mlx.md) for model sizing and details.

### 13. Headroom routing (opt-in with `--with-headroom`)
Only runs when `--with-headroom` is passed, and only for the `openrouter`
provider with a key. Points Hermes's outbound chat through the Headroom
context-compression proxy (`model.provider=custom`, `model.base_url=http://headroom:8787/v1`)
and restarts Hermes — the same thing `make headroom` does. Revert with
`make headroom-revert`.

### 14. Baserow (opt-in with `--with-baserow`)
Only runs when `--with-baserow` is passed. Adds
[`docker-compose.baserow.yml`](docker-compose.baserow.yml) to `COMPOSE_FILE` so
the all-in-one Baserow image + its dedicated `baserow_db`/`baserow_redis` come up
with the stack — UI/REST API at `http://baserow.localhost`. AI fields default to
the local Ollama container. The same thing `make baserow` does; revert (and drop
the override) with `make baserow-revert`. See
[README → Baserow](README.md#baserow-structured-data).

### 15. Directus (opt-in with `--with-directus`)
Only runs when `--with-directus` is passed. Adds
[`docker-compose.directus.yml`](docker-compose.directus.yml) to `COMPOSE_FILE` so
Directus comes up against the shared `collection_db` — UI/REST/GraphQL API at
`http://directus.localhost`, logging in with `DIRECTUS_ADMIN_EMAIL` /
`DIRECTUS_ADMIN_PASSWORD`. The same thing `make directus` does; revert (and
drop the override) with `make directus-revert`. MCP access is enabled in the
Directus UI itself (Settings → AI), not scriptable here. See
[README → Directus](README.md#directus-triage-ui).

### 16. Observability (opt-in with `--with-observability`)
Only runs when `--with-observability` is passed. Adds
[`docker-compose.observability.yml`](docker-compose.observability.yml) to
`COMPOSE_FILE` so Prometheus, Grafana, cAdvisor, the exporters, Alertmanager,
and Loki/Promtail come up — dashboards at `http://grafana.localhost` (`admin` /
`GRAFANA_ADMIN_PASSWORD`). The same thing `make observability` does; revert
(and drop the override) with `make observability-revert`. See
[README → Observability](README.md#observability-optional).

### 17. Ollama (opt-in with `--with-ollama`, or use `--external-ollama`)
Without either flag, `HINDSIGHT_LLM_BASE_URL` / `BASEROW_OLLAMA_HOST` already
default to `http://host.docker.internal:11434[/v1]` (an Ollama already running
on the Docker host) — this step only runs when `--with-ollama` is passed (or
implied by `--gpu`). It adds
[`docker-compose.ollama.yml`](docker-compose.ollama.yml) to `COMPOSE_FILE` so
the local Ollama container comes up — inference at `http://ollama.localhost`
— and repoints those two vars at it. The same thing `make ollama` does;
revert (and drop the override) with `make ollama-revert`. Pass
`--external-ollama <url>` instead to point at a URL other than the
Docker-host default. See
[README → Ollama (optional)](README.md#ollama-optional).

## Re-running / repair

Because every step is idempotent you can re-run the installer to:

- add a key or Telegram config you initially skipped,
- recover after editing `.env`,
- re-apply Windmill/MCP wiring.

Combine with `--no-pull` to skip image downloads when nothing changed.

## See also

- [README.md](README.md) — full stack overview and per-component docs.
- The interactive alternative: `make bootstrap` (uses the Hermes setup wizard).
