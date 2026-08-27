#!/usr/bin/env bash
# =============================================================================
#  HermesFlow — non-interactive installer
#
#  Stands up the whole stack without the interactive Hermes wizard. Safe to
#  re-run (idempotent): it only fills blanks, never clobbers existing secrets.
#
#  Quick start:
#    OPENROUTER_API_KEY=sk-or-... ./install.sh
#    ./install.sh --provider openrouter --api-key sk-or-... --model openai/gpt-4o-mini
#
#  Profiles (presets; explicit flags still override them):
#    --profile minimal   --no-memory --no-windmill (just the gateway + provider)
#    --profile full       memory + windmill + --with-headroom + --with-ollama
#    --profile gpu        --gpu (Linux NVIDIA host, in-container Ollama; implies --with-ollama)
#    --profile mac        --hindsight-model qwen2.5:3b + --with-ollama (Apple Silicon, RAM-friendly)
#    --profile server     --bind-lan (LAN exposure + auto Hindsight API key)
#    --profile remote     route Hindsight at the cloud provider — no local Ollama
#                         models (for low-powered hosts)
#
#  --dry-run                print the resolved plan and exit without changing anything
#
#  What it does (mirrors `make bootstrap`, minus the TTY wizard):
#    1. check prerequisites (docker, compose, openssl, curl, make)
#    2. validate the model against the provider's /models list (--skip-model-check
#       to bypass)
#    3. create .env from .env.example
#    4. set HERMES_UID/GID to the host user (non-Windows)
#    5. generate every required secret (make secrets)
#    6. create data dirs + fix ownership (make init)
#    7. write the provider key into <DATA_DIR>/.env — the file Hermes reads,
#       the same one the wizard would produce
#    8. pull images, build the local Hermes image, + start the stack
#    9. set the default model and probe Hermes end-to-end
#   10. pull Hindsight's Ollama models + enable it as the memory provider
#       (--no-memory to skip)
#   11. prep Windmill: pre-install the worker Python, create the 'main'
#       workspace, and register Windmill with Hermes over MCP
#       (--no-windmill to skip)
#
#  Optional Telegram channel (both required together):
#    --telegram-bot-token <token>          BotFather token
#    --telegram-allowed-users <id,id,...>  numeric user IDs allowed to talk to it
#
#  Optional MLX host inference server (Apple Silicon macOS only):
#    --with-mlx                            install mlx-lm + always-on launchd agent
#
#  Optional Hindsight (memory) model overrides — written to .env before 'up':
#    --hindsight-model <id>                set every Hindsight LLM scope to <id>
#    --hindsight-retain-model <id>         override just the retain scope
#    --hindsight-consolidation-model <id>  override just the consolidation scope
#    --hindsight-reflect-model <id>        override just the reflect scope
#    --hindsight-base-url <url>            Hindsight LLM endpoint (ollama/LMStudio/MLX)
#    --hindsight-mlx                       point Hindsight at the host MLX server
#    --hindsight-api-key <key>             bearer token protecting the Hindsight API
#
#  Other optional channels / toggles:
#    --no-build                            skip building the local Hermes image
#                                          (use only if it's already built)
#    --discord-bot-token <token>           Discord bot token (needs allowed-users)
#    --discord-allowed-users <id,id,...>   Discord user IDs allowed to use the bot
#    --with-headroom                       route Hermes through the Headroom proxy
#    --with-baserow                        add Baserow (structured-data UI + REST API)
#    --with-directus                       add Directus (triage UI + REST/GraphQL API + MCP)
#    --with-observability                  add Prometheus/Grafana/exporters/Loki+Promtail
#    --with-ollama                         add a local Ollama container (docker-compose.ollama.yml);
#                                          default (neither flag) assumes one already runs on the
#                                          Docker host, at http://host.docker.internal:11434
#    --external-ollama <url>               use an Ollama at a URL other than the Docker-host
#                                          default above (a different LAN box, a non-standard
#                                          port) — mutually exclusive with --with-ollama
#    --bind-lan                            expose Hermes/Hindsight/Ollama on 0.0.0.0
#    --gpu                                 NVIDIA GPU passthrough for Ollama (Linux; implies --with-ollama)
#    --env KEY=VALUE                       set any other .env var (repeatable)
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# ── defaults / args ──────────────────────────────────────────────────────────
PROVIDER="openrouter"
API_KEY=""
MODEL=""
DO_PULL=1
DO_BUILD=1
CHECK_MODEL=1
WITH_MEMORY=1
WITH_WINDMILL=1
WITH_MLX=0
TG_TOKEN=""
TG_USERS=""
HS_MODEL=""
HS_RETAIN=""
HS_CONSOLIDATION=""
HS_REFLECT=""
HS_BASE_URL=""
HS_API_KEY=""
HS_MLX=0
DISCORD_TOKEN=""
DISCORD_USERS=""
WITH_HEADROOM=0
WITH_BASEROW=0
WITH_DIRECTUS=0
WITH_OBSERVABILITY=0
WITH_OLLAMA=0
EXTERNAL_OLLAMA=""
BIND_LAN=0
GPU=0
PROFILE=""
DRY_RUN=0
HS_REMOTE=0
EXTRA_ENV=()

# Install profiles: presets applied BEFORE explicit flags (so flags override).
# Pre-scan argv for --profile <name> (space form), then seed the matching vars.
prev=""; for a in "$@"; do [ "$prev" = "--profile" ] && PROFILE="$a"; prev="$a"; done
case "$PROFILE" in
  "")      : ;;
  minimal) WITH_MEMORY=0; WITH_WINDMILL=0 ;;
  full)    WITH_HEADROOM=1; WITH_OLLAMA=1 ;;
  gpu)     GPU=1; WITH_OLLAMA=1 ;;
  mac)     HS_MODEL="qwen2.5:3b"; WITH_OLLAMA=1 ;;
  server)  BIND_LAN=1 ;;
  remote)  HS_REMOTE=1 ;;
  *) echo "✗ unknown --profile '$PROFILE' (minimal|full|gpu|mac|server|remote)" >&2; exit 1 ;;
esac
[ -n "$PROFILE" ] && echo "→ applying profile '$PROFILE'"

usage() {
  # Print the header comment block (between the two ==== dividers).
  sed -n '3,/^# =\{20\}/p' "$0" | sed 's/^# \{0,1\}//; /^=\{20\}/d'
  exit "${1:-0}"
}

# Fetch the provider's model ids, one per line. Echoes nothing on failure.
fetch_model_ids() {
  local body
  if [ "$AUTH_STYLE" = "anthropic" ]; then
    body="$(curl -fsS --max-time 20 "$MODELS_URL" \
      -H "x-api-key: $API_KEY" -H "anthropic-version: 2023-06-01" 2>/dev/null)" || return 1
  else
    body="$(curl -fsS --max-time 20 "$MODELS_URL" \
      -H "Authorization: Bearer $API_KEY" 2>/dev/null)" || return 1
  fi
  # Pull every "id":"..." value without depending on jq.
  printf '%s' "$body" \
    | grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | sed -E 's/.*"([^"]+)"$/\1/'
}

# Set KEY=VALUE in an env file (replace an existing line or append). Uses
# grep+append rather than sed so values with :,/ etc. need no escaping.
dataenv_set() {
  local file="$1" key="$2" val="$3"
  mkdir -p "$(dirname "$file")"
  if [ -f "$file" ] && grep -qE "^$key=" "$file"; then
    grep -vE "^$key=" "$file" > "$file.tmp" && mv "$file.tmp" "$file"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$file"
}

# Set KEY=VALUE in the top-level .env in place (preserve position; append if new).
# Safe for model ids / URLs (no '|' in values, so the sed delimiter is fine).
env_put() {
  local key="$1" val="$2"
  if grep -qE "^$key=" .env 2>/dev/null; then
    sed -i.bak "s|^$key=.*|$key=$val|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

# Append an optional override file to COMPOSE_FILE in .env, additively and
# idempotently (so --gpu and --with-baserow can both be active). Seeds the base
# docker-compose.yml when COMPOSE_FILE is unset.
compose_add() {
  local f="$1" cur
  # `|| true` matters: under `set -e -o pipefail`, a fresh .env with no
  # COMPOSE_FILE= line yet makes grep exit 1 (no match), which without this
  # guard silently kills the whole installer right here.
  cur="$(grep -E '^COMPOSE_FILE=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
  case ":$cur:" in
    *":$f:"*) return 0 ;;
    *) if [ -z "$cur" ]; then cur="docker-compose.yml:$f"; else cur="$cur:$f"; fi ;;
  esac
  env_put COMPOSE_FILE "$cur"
}

# Block until the hermes container passes its healthcheck (or give up).
wait_hermes_healthy() {
  echo "→ waiting for Hermes to become healthy…"
  local s
  for _ in $(seq 1 40); do
    s="$(docker inspect -f '{{.State.Health.Status}}' hermes 2>/dev/null || echo none)"
    [ "$s" = "healthy" ] && { echo "✓ Hermes healthy"; return 0; }
    sleep 5
  done
  echo "✗ Hermes did not become healthy — check 'docker logs hermes'" >&2
  exit 1
}

# Pull the Ollama models Hindsight uses for fact extraction / consolidation /
# reflection — embeddings are local (BAAI/bge-small-en-v1.5) so no embedding
# model is needed. Branches on where Ollama actually lives: the bundled
# container (docker exec), an external one (--external-ollama, over HTTP), or
# neither (skip — Hindsight isn't on Ollama).
pull_hindsight_models() {
  if [ -n "$EXTERNAL_OLLAMA" ]; then
    pull_hindsight_models_external
    return
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx ollama; then
    echo "→ no local Ollama container running — skipping model pull"
    return 0
  fi
  local models m
  models="$(printf '%s\n' \
      "${HINDSIGHT_LLM_MODEL:-}" \
      "${HINDSIGHT_RETAIN_LLM_MODEL:-}" \
      "${HINDSIGHT_CONSOLIDATION_LLM_MODEL:-}" \
      "${HINDSIGHT_REFLECT_LLM_MODEL:-}" \
    | sed '/^[[:space:]]*$/d' | sort -u)"
  [ -n "$models" ] || { echo "→ no Hindsight Ollama models configured — skipping"; return 0; }
  for m in $models; do
    if docker exec ollama ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      echo "✓ Ollama model already present: $m"
    else
      echo "→ pulling Ollama model '$m' (can be large/slow)…"
      docker exec ollama ollama pull "$m" \
        || echo "⚠ failed to pull '$m' — Hindsight extraction won't work until it's available"
    fi
  done
}

# Same as above, but against an Ollama reachable only over HTTP (no docker exec).
pull_hindsight_models_external() {
  local models m url present
  url="${EXTERNAL_OLLAMA%/}"
  models="$(printf '%s\n' \
      "${HINDSIGHT_LLM_MODEL:-}" \
      "${HINDSIGHT_RETAIN_LLM_MODEL:-}" \
      "${HINDSIGHT_CONSOLIDATION_LLM_MODEL:-}" \
      "${HINDSIGHT_REFLECT_LLM_MODEL:-}" \
    | sed '/^[[:space:]]*$/d' | sort -u)"
  [ -n "$models" ] || { echo "→ no Hindsight Ollama models configured — skipping"; return 0; }
  # `|| true` matters here too: curl failing (unreachable host) or grep finding
  # no "name" fields (no models pulled yet) would otherwise abort the script
  # under `set -e -o pipefail` — either case should just mean "nothing present".
  present="$( { curl -fsS --max-time 10 "$url/api/tags" 2>/dev/null \
    | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*"([^"]+)"$/\1/' ; } || true)"
  for m in $models; do
    if printf '%s\n' "$present" | grep -qx "$m"; then
      echo "✓ Ollama model already present: $m"
    else
      echo "→ pulling Ollama model '$m' on external Ollama (can be large/slow)…"
      curl -fsS --max-time 1800 -X POST "$url/api/pull" -H 'Content-Type: application/json' \
          -d "{\"name\":\"$m\"}" >/dev/null \
        || echo "⚠ failed to pull '$m' — Hindsight extraction won't work until it's available"
    fi
  done
}

# Push the tracked windmill/ assets (resource type, resource, example scripts)
# to the server with the `wmill` CLI, then seed the secret variable that sync
# intentionally skips. Best-effort and idempotent: if the CLI isn't installed we
# print how to do it by hand and move on — the installer never hard-depends on
# node/npm. Args: $1 = admin bearer token, $2 = base URL, $3 = Host header.
push_windmill_assets() {
  local token="$1" base="$2" hh="$3" remote
  if ! command -v wmill >/dev/null 2>&1; then
    echo "→ Windmill assets not pushed: 'wmill' CLI not found."
    echo "  Install it (npm install -g windmill-cli) then run:  make windmill-push"
    return 0
  fi
  # The CLI talks to the workspace's public URL (it can't replay the Host-header
  # trick the API calls use), so it relies on windmill.localhost resolving.
  remote="${WM_BASE_URL:-http://windmill.localhost}"
  echo "→ pushing windmill/ assets to $remote (workspace 'main')…"
  # Register a CLI profile non-interactively with the admin token. Idempotent:
  # re-adding an existing profile just refreshes it; ignore a benign failure.
  ( cd windmill && wmill workspace add main main "$remote" --token "$token" >/dev/null 2>&1 ) || true
  # Regenerate .script.yaml + lockfiles so new/edited scripts lock cleanly
  # (needs the server, which is up here). Best-effort: don't block the push.
  # generate-metadata's static import analysis misses imports made inside
  # function bodies and can silently empty an otherwise-correct lock file
  # (hit this with f/collection/baserow_webhook and f/data_platform/dbt_run —
  # see docs/data-platform-add-pipeline.md), so snapshot lock files first and
  # restore any that shrank.
  local lock_backup
  lock_backup=$(mktemp -d)
  ( cd windmill && find . -name '*.script.lock' ) | while read -r f; do
    mkdir -p "$lock_backup/$(dirname "$f")"; cp "windmill/$f" "$lock_backup/$f"
  done
  ( cd windmill && wmill generate-metadata >/dev/null 2>&1 ) || true
  ( cd windmill && find . -name '*.script.lock' ) | while read -r f; do
    [ -f "$lock_backup/$f" ] || continue
    old_n=$(grep -vc '^#' "$lock_backup/$f" 2>/dev/null || echo 0)
    new_n=$(grep -vc '^#' "windmill/$f" 2>/dev/null || echo 0)
    if [ "$old_n" -gt 0 ] && [ "$new_n" -lt "$old_n" ]; then
      echo "⚠ generate-metadata emptied windmill/$f's pinned deps ($old_n → $new_n) — restoring (see docs/data-platform-add-pipeline.md)"
      cp "$lock_backup/$f" "windmill/$f"
    fi
  done
  rm -rf "$lock_backup"
  # Safety: `wmill sync push` mirrors local→remote and DELETES/ARCHIVES any
  # remote item absent locally. Dry-run first and refuse if anything would be
  # removed, so re-running the installer can't wipe assets built in the UI.
  # Override deliberately with WMILL_FORCE_PUSH=1.
  local esc dels
  esc=$(printf '\033')
  dels="$( ( cd windmill && wmill sync push --dry-run --yes --skip-branch-validation 2>&1 ) \
    | sed "s/${esc}\[[0-9;]*m//g" \
    | grep -E '^- (folder|variable|resource|resource-type|script|flow|app|schedule|trigger|user|group|settings)( |$)' || true)"
  if [ -n "$dels" ] && [ "${WMILL_FORCE_PUSH:-0}" != "1" ]; then
    echo "⚠ Windmill push skipped — it would DELETE/ARCHIVE remote items not tracked in windmill/:"
    printf '%s\n' "$dels" | sed 's/^/    /'
    echo "  Bring them into the repo first:  make windmill-pull"
    echo "  Or mirror anyway (destructive):  WMILL_FORCE_PUSH=1 ./install.sh …"
    return 0
  fi
  if ( cd windmill && wmill sync push --yes --skip-branch-validation >/dev/null 2>&1 ); then
    echo "✓ pushed windmill/ assets (resource type, f/hermes/local, client.py, chat.py)"
  else
    echo "⚠ 'wmill sync push' failed — run it by hand from windmill/ (see README 'Push it')."
    return 0
  fi
  # sync keeps secrets out of git (skipSecrets: true), so f/hermes/api_key is
  # never pushed. Seed it server-side from API_SERVER_KEY so the resource works.
  # (Not $API_KEY — that shell var holds the LLM provider key, e.g. an
  # OpenRouter key; API_SERVER_KEY is the separate Hermes gateway key and is
  # already exported by the `set -a; . ./.env; set +a` sourcing above.)
  if [ -n "${API_SERVER_KEY:-}" ]; then
    local vp="f/hermes/api_key"
    # create, else update if it already exists — keeps the value current on re-runs.
    if curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/w/main/variables/create" \
         -d "{\"path\":\"$vp\",\"value\":\"$API_SERVER_KEY\",\"is_secret\":true,\"description\":\"Hermes gateway API_SERVER_KEY\"}" >/dev/null 2>&1; then
      echo "✓ created secret variable $vp"
    elif curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/w/main/variables/update/$vp" \
         -d "{\"value\":\"$API_SERVER_KEY\"}" >/dev/null 2>&1; then
      echo "✓ updated secret variable $vp"
    else
      echo "⚠ couldn't set $vp — set it in the UI (Variables → $vp) to your API_SERVER_KEY."
    fi
  fi
  # Same idea for the Telegram bot token and allow-list, so Windmill scripts can
  # send messages/documents to Telegram without their own copy of these secrets.
  # TG_TOKEN/TG_USERS (flag, or env-var fallback) are resolved once near the top
  # of the script and are already final by the time setup_windmill() runs.
  if [ -n "${TG_TOKEN:-}" ]; then
    local vp="f/hermes/telegram_bot_token"
    if curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/w/main/variables/create" \
         -d "{\"path\":\"$vp\",\"value\":\"$TG_TOKEN\",\"is_secret\":true,\"description\":\"Telegram bot token, used by Windmill scripts sending messages/documents to Telegram\"}" >/dev/null 2>&1; then
      echo "✓ created secret variable $vp"
    elif curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/w/main/variables/update/$vp" \
         -d "{\"value\":\"$TG_TOKEN\"}" >/dev/null 2>&1; then
      echo "✓ updated secret variable $vp"
    else
      echo "⚠ couldn't set $vp — set it in the UI (Variables → $vp) to your Telegram bot token."
    fi
  fi
  if [ -n "${TG_USERS:-}" ]; then
    local vp="f/hermes/telegram_allow_user"
    if curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/w/main/variables/create" \
         -d "{\"path\":\"$vp\",\"value\":\"$TG_USERS\",\"is_secret\":true,\"description\":\"Comma-separated Telegram user IDs allowed to receive documents from Windmill scripts\"}" >/dev/null 2>&1; then
      echo "✓ created secret variable $vp"
    elif curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/w/main/variables/update/$vp" \
         -d "{\"value\":\"$TG_USERS\"}" >/dev/null 2>&1; then
      echo "✓ updated secret variable $vp"
    else
      echo "⚠ couldn't set $vp — set it in the UI (Variables → $vp) to your Telegram allowed users."
    fi
  fi
}

# Prepare Windmill: ensure a 'main' workspace exists, push the tracked assets.
#
# Pre-installing the worker Python used to happen here, but that race is now
# handled by the `windmill_cache_init` service in docker-compose.yml, which
# validates/repairs the shared interpreter cache before any worker replica
# starts — on every `docker compose up`, not just at install time.
setup_windmill() {
  # Ensure a 'main' workspace exists. A fresh Windmill CE has none, and
  # `wmill workspace add` only registers it locally — it does not create it
  # server-side, so the first `wmill sync push` would fail without this.
  local base hh token i
  base="http://127.0.0.1:${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"
  for i in $(seq 1 24); do
    curl -fsS -H "Host: $hh" "$base/api/version" >/dev/null 2>&1 && break
    sleep 5
  done
  token="$(curl -fsS -H "Host: $hh" -H 'Content-Type: application/json' \
    -X POST "$base/api/auth/login" \
    -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null)"
  if [ -z "$token" ]; then
    echo "→ Windmill: default admin login didn't work (already customized?) — skipping workspace setup."
    return 0
  fi
  if curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" \
       "$base/api/workspaces/list" 2>/dev/null | grep -q '"id":"main"'; then
    echo "✓ Windmill 'main' workspace already exists"
  elif curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
         -X POST "$base/api/workspaces/create" -d '{"id":"main","name":"main"}' >/dev/null 2>&1; then
    echo "✓ created Windmill 'main' workspace"
  else
    echo "⚠ couldn't create the Windmill 'main' workspace — create it in the UI before 'wmill sync push'."
  fi

  # Ensure runtime-state folders exist (create-or-no-op). Both stay outside
  # sync scope: hermes_state holds Hermes cursors; hermes_flow_state holds
  # lifecycle deprecation/rollback audit records.
  for state_folder in hermes_state hermes_flow_state; do
    if curl -fsS -o /dev/null -H "Host: $hh" -H "Authorization: Bearer $token" \
         "$base/api/w/main/folders/get/$state_folder" 2>/dev/null; then
      echo "✓ Windmill folder f/$state_folder already exists"
    elif curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
           -X POST "$base/api/w/main/folders/create" -d "{\"name\":\"$state_folder\"}" >/dev/null 2>&1; then
      echo "✓ created Windmill folder f/$state_folder (runtime state; not synced)"
    else
      echo "⚠ couldn't create f/$state_folder — create it in the UI (Folders → New) so scripts can store state."
    fi
  done

  # Push the tracked windmill/ assets now that the workspace exists.
  push_windmill_assets "$token" "$base" "$hh"

  # Connect Hermes to Windmill over MCP, so Windmill's scripts/flows AND its
  # management API become callable tools inside Hermes sessions. Needs a token
  # with the 'mcp:all' scope (a plain token can initialize but not list tools),
  # passed as a Bearer header. Hermes reaches windmill_server directly over the
  # shared docker networks. Idempotent: skip if already configured.
  if docker exec hermes hermes mcp list 2>/dev/null | grep -qw windmill; then
    echo "✓ Hermes already has the 'windmill' MCP server configured"
    return 0
  fi
  local mcptoken
  mcptoken="$(curl -fsS -H "Host: $hh" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -X POST "$base/api/users/tokens/create" -d '{"label":"hermes-mcp","scopes":["mcp:all"]}' 2>/dev/null)"
  if [ -z "$mcptoken" ]; then
    echo "⚠ couldn't mint a Windmill MCP token — skipping Hermes↔Windmill MCP wiring."
    return 0
  fi
  # `hermes mcp add` is interactive: 'y' (server needs auth) then the token.
  if printf 'y\n%s\n' "$mcptoken" | docker exec -i hermes hermes mcp add windmill \
       --url "http://windmill_server:8000/api/mcp/w/main/sse" --auth header >/dev/null 2>&1; then
    echo "✓ registered Windmill as an MCP server in Hermes (scripts/flows + admin API as tools)"
  else
    echo "⚠ couldn't register the Windmill MCP server in Hermes — wire it up manually (see README)."
  fi
}

# Set up the host-native MLX inference server (Apple Silicon only). MLX must run
# on the host, not in a container — Docker Desktop on macOS doesn't pass the GPU
# through. Installs mlx-lm into a venv and registers the always-on launchd agent.
setup_mlx() {
  if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "⚠ --with-mlx is for Apple Silicon macOS only (host is $(uname -s)/$(uname -m)) — skipping."
    return 0
  fi
  local venv="${MLX_VENV_DIR:-$HOME/.mlx-venv}"
  echo "→ setting up host-native MLX server (Apple Silicon)…"
  if [ ! -x "$venv/bin/mlx_lm.server" ]; then
    python3 -m venv "$venv" || { echo "⚠ could not create venv at $venv — skipping MLX."; return 0; }
    "$venv/bin/pip" install -U pip >/dev/null 2>&1 || true
    if ! "$venv/bin/pip" install -U mlx-lm; then
      echo "⚠ failed to install mlx-lm — skipping MLX."; return 0
    fi
  fi
  echo "✓ mlx-lm installed in $venv"
  if MLX_VENV_BIN="$venv/bin" bash ./mlx/install-launchd.sh; then
    echo "✓ MLX server installed as an always-on launchd agent (model loads on first request)"
  else
    echo "⚠ launchd install failed — start it manually with: ./mlx/serve.sh"
  fi
  echo "  To route Hermes through MLX:    make mlx"
  echo "  Or point Hindsight at MLX:      set HINDSIGHT_LLM_BASE_URL=\${MLX_BASE_URL} in .env, then: docker restart hindsight"
}

# Verify $MODEL is offered by $PROVIDER; exit early with a helpful message if not.
validate_model() {
  [ "$CHECK_MODEL" -eq 1 ] || { echo "→ skipping model check (--skip-model-check)"; return 0; }
  [ -n "$API_KEY" ] || { echo "→ no API key — skipping model check"; return 0; }

  echo "→ validating model '$MODEL' against ${PROVIDER}…"
  local ids; ids="$(fetch_model_ids)"
  if [ -z "$ids" ]; then
    echo "⚠ could not fetch $PROVIDER model list (network/auth?) — skipping check."
    echo "  The end-to-end probe later will still catch a bad model id."
    return 0
  fi
  if printf '%s\n' "$ids" | grep -qxF "$MODEL"; then
    # Presence in the catalog ≠ callable on your key/tier — the end-to-end
    # probe at the end is the real test. This just rules out typos/bad ids.
    echo "✓ model '$MODEL' is listed by $PROVIDER"
    return 0
  fi
  echo "✗ model '$MODEL' is not offered by $PROVIDER." >&2
  # Suggest near matches: try the basename, then a coarser token (drop the last
  # '-segment'); fall back to a plain sample so the user always sees options.
  local base coarse hits
  base="$(printf '%s' "$MODEL" | sed -E 's#.*/##')"   # e.g. gpt-4o-typo
  coarse="${base%-*}"                                  # e.g. gpt-4o
  hits="$( { printf '%s\n' "$ids" | grep -iF "$base"  || true; } | head -8)"
  [ -n "$hits" ] || hits="$( { printf '%s\n' "$ids" | grep -iF "$coarse" || true; } | head -8)"
  [ -n "$hits" ] || hits="$(printf '%s\n' "$ids" | head -8)"
  echo "  Did you mean one of:" >&2
  printf '%s\n' "$hits" | sed 's/^/    /' >&2
  echo "  Full list: curl -s $MODELS_URL -H 'Authorization: Bearer <key>'" >&2
  echo "  Re-run with --model <id>, or --skip-model-check to bypass." >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --provider) PROVIDER="$2"; shift 2 ;;
    --api-key)  API_KEY="$2";  shift 2 ;;
    --model)    MODEL="$2";    shift 2 ;;
    --no-pull)  DO_PULL=0;     shift ;;
    --no-build) DO_BUILD=0;    shift ;;
    --skip-model-check) CHECK_MODEL=0; shift ;;
    --no-memory) WITH_MEMORY=0; shift ;;
    --no-windmill) WITH_WINDMILL=0; shift ;;
    --telegram-bot-token) TG_TOKEN="$2"; shift 2 ;;
    --telegram-allowed-users) TG_USERS="$2"; shift 2 ;;
    --with-mlx) WITH_MLX=1; shift ;;
    --hindsight-model) HS_MODEL="$2"; shift 2 ;;
    --hindsight-retain-model) HS_RETAIN="$2"; shift 2 ;;
    --hindsight-consolidation-model) HS_CONSOLIDATION="$2"; shift 2 ;;
    --hindsight-reflect-model) HS_REFLECT="$2"; shift 2 ;;
    --hindsight-base-url) HS_BASE_URL="$2"; shift 2 ;;
    --hindsight-api-key) HS_API_KEY="$2"; shift 2 ;;
    --hindsight-mlx) HS_MLX=1; shift ;;
    --discord-bot-token) DISCORD_TOKEN="$2"; shift 2 ;;
    --discord-allowed-users) DISCORD_USERS="$2"; shift 2 ;;
    --with-headroom) WITH_HEADROOM=1; shift ;;
    --with-baserow) WITH_BASEROW=1; shift ;;
    --with-directus) WITH_DIRECTUS=1; shift ;;
    --with-observability) WITH_OBSERVABILITY=1; shift ;;
    --with-ollama) WITH_OLLAMA=1; shift ;;
    --external-ollama) EXTERNAL_OLLAMA="$2"; shift 2 ;;
    --bind-lan) BIND_LAN=1; shift ;;
    --gpu) GPU=1; shift ;;
    --profile) shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --env) EXTRA_ENV+=("$2"); shift 2 ;;
    -h|--help)  usage 0 ;;
    *) echo "✗ unknown argument: $1" >&2; usage 1 ;;
  esac
done

# Map provider → key var, a sane default model, and the /models list endpoint.
# AUTH_STYLE selects the auth header: "bearer" (OpenAI-compatible) or "anthropic".
case "$PROVIDER" in
  openrouter)
    KEY_VAR="OPENROUTER_API_KEY"; DEFAULT_MODEL="openai/gpt-4o-mini"
    MODELS_URL="https://openrouter.ai/api/v1/models"; AUTH_STYLE="bearer" ;;
  anthropic)
    KEY_VAR="ANTHROPIC_API_KEY"; DEFAULT_MODEL="claude-sonnet-4-6"
    MODELS_URL="https://api.anthropic.com/v1/models"; AUTH_STYLE="anthropic" ;;
  openai)
    KEY_VAR="OPENAI_API_KEY"; DEFAULT_MODEL="gpt-4o-mini"
    MODELS_URL="https://api.openai.com/v1/models"; AUTH_STYLE="bearer" ;;
  *) echo "✗ unsupported --provider '$PROVIDER' (openrouter|anthropic|openai)" >&2; exit 1 ;;
esac
MODEL="${MODEL:-$DEFAULT_MODEL}"

# Load existing project values before resolving optional channel defaults. This
# keeps a re-run non-destructive while still allowing process env vars to fill
# in values when .env does not exist yet.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# Accept the key from the matching env var if not passed explicitly.
if [ -z "$API_KEY" ]; then
  API_KEY="$(printenv "$KEY_VAR" 2>/dev/null || true)"
fi

# Telegram (optional): bot token + allowed user IDs. Fall back to env vars.
[ -n "$TG_TOKEN" ] || TG_TOKEN="$(printenv TELEGRAM_BOT_TOKEN 2>/dev/null || true)"
[ -n "$TG_USERS" ] || TG_USERS="$(printenv TELEGRAM_ALLOWED_USERS 2>/dev/null || true)"
# Both are required together: an allow-list is mandatory for the Telegram channel
# (otherwise anyone who finds the bot could talk to your agent).
if { [ -n "$TG_TOKEN" ] || [ -n "$TG_USERS" ]; } && { [ -z "$TG_TOKEN" ] || [ -z "$TG_USERS" ]; }; then
  echo "✗ Telegram needs BOTH --telegram-bot-token and --telegram-allowed-users" >&2
  echo "  (allowed user IDs are required for the Hermes Telegram channel)." >&2
  exit 1
fi

# Discord (optional): same both-required rule as Telegram.
[ -n "$DISCORD_TOKEN" ] || DISCORD_TOKEN="$(printenv DISCORD_BOT_TOKEN 2>/dev/null || true)"
[ -n "$DISCORD_USERS" ] || DISCORD_USERS="$(printenv DISCORD_ALLOWED_USERS 2>/dev/null || true)"
if { [ -n "$DISCORD_TOKEN" ] || [ -n "$DISCORD_USERS" ]; } && { [ -z "$DISCORD_TOKEN" ] || [ -z "$DISCORD_USERS" ]; }; then
  echo "✗ Discord needs BOTH --discord-bot-token and --discord-allowed-users" >&2
  echo "  (allowed user IDs are required for the Hermes Discord channel)." >&2
  exit 1
fi

# --with-ollama and --external-ollama both answer "where does Ollama live?" —
# only one can be true.
if [ "$WITH_OLLAMA" -eq 1 ] && [ -n "$EXTERNAL_OLLAMA" ]; then
  echo "✗ --with-ollama and --external-ollama are mutually exclusive — pick one." >&2
  exit 1
fi

# --gpu only makes sense with the bundled Ollama container running.
if [ "$GPU" -eq 1 ] && [ "$(uname -s)" != "Darwin" ]; then
  WITH_OLLAMA=1
fi

# --hindsight-mlx: point Hindsight's extraction LLM at the host MLX server (uses
# MLX_BASE_URL / MLX_MODEL or their defaults; explicit --hindsight-* flags win).
if [ "$HS_MLX" -eq 1 ]; then
  [ -z "$HS_BASE_URL" ] && HS_BASE_URL="${MLX_BASE_URL:-http://host.docker.internal:8080/v1}"
  [ -z "$HS_MODEL" ] && HS_MODEL="${MLX_MODEL:-mlx-community/Qwen2.5-7B-Instruct-4bit}"
fi

# 'remote' profile: route Hindsight's extraction LLM at the cloud provider so no
# local Ollama models are needed (good for low-powered hosts). Needs an
# OpenAI-compatible provider; explicit --hindsight-* flags still win.
if [ "$HS_REMOTE" -eq 1 ]; then
  case "$PROVIDER" in
    openrouter) [ -z "$HS_BASE_URL" ] && HS_BASE_URL="https://openrouter.ai/api/v1"; [ -z "$HS_MODEL" ] && HS_MODEL="openai/gpt-4o-mini" ;;
    openai)     [ -z "$HS_BASE_URL" ] && HS_BASE_URL="https://api.openai.com/v1";    [ -z "$HS_MODEL" ] && HS_MODEL="gpt-4o-mini" ;;
    *) echo "⚠ --profile remote needs an OpenAI-compatible provider (openrouter/openai) for Hindsight; '$PROVIDER' isn't — leaving Hindsight on its .env backend." >&2 ;;
  esac
fi

# Dry run: print the resolved plan and exit before changing anything.
if [ "$DRY_RUN" -eq 1 ]; then
  yn() { [ "$1" -eq 1 ] && echo yes || echo no; }
  echo
  echo "Dry run — no files written, no containers touched. Planned install:"
  echo "  Provider:        $PROVIDER (api key: $([ -n "$API_KEY" ] && echo present || echo ABSENT))"
  echo "  Default model:   $MODEL"
  echo "  Profile:         ${PROFILE:-none}"
  echo "  Secrets:         generate any blank/weak of API_SERVER_KEY, WM_DB_PASSWORD, HINDSIGHT_DB_PASSWORD, GRAFANA_ADMIN_PASSWORD, COLLECTION_DB_ADMIN_PASSWORD, BASEROW_*, DIRECTUS_*, WINDMILL_COLLECTION_DB_PASSWORD, HERMES_DASHBOARD_BASIC_AUTH_*"
  echo "  Memory:          $(yn $WITH_MEMORY)$([ "$HS_REMOTE" -eq 1 ] && echo " (remote via $PROVIDER)")"
  [ -n "$HS_MODEL$HS_BASE_URL" ] && echo "  Hindsight LLM:   model=${HS_MODEL:-<.env>} base=${HS_BASE_URL:-<.env>}"
  echo "  Windmill:        $(yn $WITH_WINDMILL)"
  echo "  MLX server:      $(yn $WITH_MLX)    Headroom: $(yn $WITH_HEADROOM)    GPU passthrough: $(yn $GPU)    LAN bind: $(yn $BIND_LAN)"
  echo "  Baserow:         $(yn $WITH_BASEROW)$([ "$WITH_BASEROW" -eq 1 ] && echo " (docker-compose.baserow.yml)")"
  echo "  Directus:        $(yn $WITH_DIRECTUS)$([ "$WITH_DIRECTUS" -eq 1 ] && echo " (docker-compose.directus.yml)")"
  echo "  Observability:   $(yn $WITH_OBSERVABILITY)$([ "$WITH_OBSERVABILITY" -eq 1 ] && echo " (docker-compose.observability.yml)")"
  if [ -n "$EXTERNAL_OLLAMA" ]; then
    echo "  Ollama:          external ($EXTERNAL_OLLAMA)"
  else
    echo "  Ollama:          $(yn $WITH_OLLAMA)$([ "$WITH_OLLAMA" -eq 1 ] && echo " (docker-compose.ollama.yml)")"
  fi
  echo "  Telegram:        $([ -n "$TG_TOKEN" ] && echo configured || echo none)    Discord: $([ -n "$DISCORD_TOKEN" ] && echo configured || echo none)"
  [ "${#EXTRA_ENV[@]}" -gt 0 ] && echo "  Extra .env:      ${EXTRA_ENV[*]}"
  echo "  Steps:           $([ "$DO_PULL" -eq 1 ] && echo 'pull → ')$([ "$DO_BUILD" -eq 1 ] && echo 'build → ')up → heal → set-model → probe$([ "$WITH_MEMORY" -eq 1 ] && echo ' → memory')$([ "$WITH_WINDMILL" -eq 1 ] && echo ' → windmill')$([ "$WITH_MLX" -eq 1 ] && echo ' → mlx')$([ "$WITH_HEADROOM" -eq 1 ] && echo ' → headroom')"
  echo
  echo "Re-run without --dry-run to apply."
  exit 0
fi

# ── 1. prerequisites ─────────────────────────────────────────────────────────
echo "→ checking prerequisites…"
for bin in docker make openssl curl; do
  command -v "$bin" >/dev/null 2>&1 || { echo "✗ '$bin' not found" >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "✗ 'docker compose' v2 not found" >&2; exit 1; }
echo "✓ prerequisites OK"

# ── 1b. validate the model against the provider before doing any real work ────
validate_model

# ── 2. .env ──────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "✓ created .env from .env.example"
else
  echo "→ .env already exists — leaving it in place"
fi

# ── 3. host UID/GID (skip on Windows, where Docker Desktop maps a virtual user)─
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) echo "→ Windows host — leaving HERMES_UID/GID at their .env values" ;;
  *)
    HU="$(id -u)"; HG="$(id -g)"
    sed -i.bak -E "s|^HERMES_UID=.*|HERMES_UID=$HU|; s|^HERMES_GID=.*|HERMES_GID=$HG|" .env && rm -f .env.bak
    echo "✓ set HERMES_UID=$HU HERMES_GID=$HG"
    ;;
esac

# ── 4b. Hindsight model / backend overrides (optional) ───────────────────────
# Written to .env before 'up' so the hindsight container starts with them, and so
# step 10 pulls the right Ollama models. --hindsight-model seeds every scope;
# per-scope flags override it.
HS_RETAIN="${HS_RETAIN:-$HS_MODEL}"
HS_CONSOLIDATION="${HS_CONSOLIDATION:-$HS_MODEL}"
HS_REFLECT="${HS_REFLECT:-$HS_MODEL}"
[ -n "$HS_MODEL" ]         && env_put HINDSIGHT_LLM_MODEL "$HS_MODEL"
[ -n "$HS_RETAIN" ]        && env_put HINDSIGHT_RETAIN_LLM_MODEL "$HS_RETAIN"
[ -n "$HS_CONSOLIDATION" ] && env_put HINDSIGHT_CONSOLIDATION_LLM_MODEL "$HS_CONSOLIDATION"
[ -n "$HS_REFLECT" ]       && env_put HINDSIGHT_REFLECT_LLM_MODEL "$HS_REFLECT"
[ -n "$HS_BASE_URL" ]      && env_put HINDSIGHT_LLM_BASE_URL "$HS_BASE_URL"
# remote profile: Hindsight authenticates to the cloud provider with the same key.
[ "$HS_REMOTE" -eq 1 ] && [ -n "$API_KEY" ] && env_put HINDSIGHT_LLM_API_KEY "$API_KEY"
# --hindsight-mlx: MLX needs no real key (placeholder).
[ "$HS_MLX" -eq 1 ] && env_put HINDSIGHT_LLM_API_KEY mlx
[ -n "$HS_MODEL$HS_RETAIN$HS_CONSOLIDATION$HS_REFLECT$HS_BASE_URL" ] \
  && echo "✓ applied Hindsight model/backend overrides to .env"

# Ollama (local LLM inference) — layer its optional compose override. Must be
# added before docker-compose.gpu.yml (below), since that file patches this
# service. .env.example defaults Hindsight/Baserow at a host-native Ollama
# (host.docker.internal), so repoint them at the bundled container here —
# unless an explicit --hindsight-base-url already won that argument.
if [ "$WITH_OLLAMA" -eq 1 ]; then
  compose_add docker-compose.ollama.yml
  [ -z "$HS_BASE_URL" ] && env_put HINDSIGHT_LLM_BASE_URL "http://ollama:11434/v1"
  env_put BASEROW_OLLAMA_HOST "http://ollama:11434"
  echo "✓ enabled Ollama — local LLM inference at http://ollama.localhost"
fi

# External Ollama (already running on the Docker host or elsewhere) — point
# Hindsight/Baserow at it instead of starting a bundled container. Skip the
# Hindsight URL if an explicit --hindsight-base-url/--hindsight-mlx/remote
# profile already set one — those are more specific and should win.
if [ -n "$EXTERNAL_OLLAMA" ]; then
  EXTERNAL_OLLAMA="${EXTERNAL_OLLAMA%/}"
  [ -z "$HS_BASE_URL" ] && env_put HINDSIGHT_LLM_BASE_URL "$EXTERNAL_OLLAMA/v1"
  env_put BASEROW_OLLAMA_HOST "$EXTERNAL_OLLAMA"
  echo "✓ pointed Hindsight/Baserow at external Ollama: $EXTERNAL_OLLAMA"
fi

# NVIDIA GPU passthrough for the ollama container (Linux / WSL2 + nvidia-container-toolkit).
if [ "$GPU" -eq 1 ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "⚠ --gpu has no effect on macOS — Docker Desktop can't pass the GPU through. Use --with-mlx instead."
  else
    compose_add docker-compose.gpu.yml
    env_put CUDA_VISIBLE_DEVICES 0
    env_put OLLAMA_NUM_GPU 999
    echo "✓ enabled NVIDIA GPU passthrough for Ollama (needs nvidia-container-toolkit on the host)"
  fi
fi

# Baserow (structured-data UI + REST API) — layer its optional compose override.
# Secrets (BASEROW_SECRET_KEY / DB / Redis passwords) are generated by
# `make secrets` below; AI fields default to the local ollama container.
if [ "$WITH_BASEROW" -eq 1 ]; then
  compose_add docker-compose.baserow.yml
  echo "✓ enabled Baserow — UI / REST API at http://baserow.localhost (first boot runs migrations)"
fi

# Directus (triage UI + REST/GraphQL API + MCP) — layer its optional compose
# override. Uses the base stack's shared collection_db (own `directus` schema
# + the shared `collection` schema). MCP is enabled via Settings -> AI in the
# Studio UI after first login, not here.
if [ "$WITH_DIRECTUS" -eq 1 ]; then
  compose_add docker-compose.directus.yml
  echo "✓ enabled Directus — UI / API at http://directus.localhost (first boot runs migrations)"
fi

# Observability (Prometheus, Grafana, exporters, Loki+Promtail) — layer its
# optional compose override. GRAFANA_ADMIN_PASSWORD is generated by
# `make secrets` below regardless, so the override can be added later without
# re-running secrets.
if [ "$WITH_OBSERVABILITY" -eq 1 ]; then
  compose_add docker-compose.observability.yml
  echo "✓ enabled observability — Grafana at http://grafana.localhost, Prometheus at http://prometheus.localhost"
fi

# Expose services on the LAN (0.0.0.0) instead of loopback only.
if [ "$BIND_LAN" -eq 1 ]; then
  env_put HERMES_BIND 0.0.0.0
  env_put HINDSIGHT_BIND 0.0.0.0
  env_put OLLAMA_BIND 0.0.0.0
  echo "✓ bound Hermes/Hindsight/Ollama to 0.0.0.0 (LAN access)"
fi

# Hindsight API bearer token: explicit value, or auto-generate when exposing to
# the LAN and none is set (don't leave the memory API open on the network).
if [ -n "$HS_API_KEY" ]; then
  env_put HINDSIGHT_API_KEY "$HS_API_KEY"
  echo "✓ set HINDSIGHT_API_KEY"
elif [ "$BIND_LAN" -eq 1 ] && [ -z "$(grep -E '^HINDSIGHT_API_KEY=' .env | cut -d= -f2-)" ]; then
  env_put HINDSIGHT_API_KEY "$(openssl rand -hex 16)"
  echo "✓ generated HINDSIGHT_API_KEY (Hindsight is exposed to the LAN)"
fi

# Generic passthrough: --env KEY=VALUE (repeatable) → write to .env before 'up'.
if [ "${#EXTRA_ENV[@]}" -gt 0 ]; then
  for kv in "${EXTRA_ENV[@]}"; do
    case "$kv" in
      *=*) env_put "${kv%%=*}" "${kv#*=}"; echo "✓ set ${kv%%=*} (--env)" ;;
      *) echo "⚠ ignoring --env '$kv' (expected KEY=VALUE)" >&2 ;;
    esac
  done
fi

# ── 4. secrets (API key + DB passwords) ──────────────────────────────────────
make --no-print-directory secrets

# ── 5. data dirs + ownership ─────────────────────────────────────────────────
make --no-print-directory init

# Resolve DATA_DIR exactly as the recipes do (Compose-style ${HOME} expansion).
set -a; . ./.env; set +a
DATA_DIR_RESOLVED="$(eval echo "${DATA_DIR:-$HOME/.hermes}")"

# ── 6. provider key → <DATA_DIR>/.env  AND top-level .env ────────────────────
# The compose `hermes` service leaves provider keys commented out and reads them
# from /opt/data/.env instead (what the wizard writes). Other services substitute
# the key from the TOP-LEVEL .env via ${OPENROUTER_API_KEY} (Headroom) and
# ${HINDSIGHT_LLM_API_KEY} (remote Hindsight) — so write both; both survive a
# redeploy.
if [ -n "$API_KEY" ]; then
  dataenv_set "$DATA_DIR_RESOLVED/.env" "$KEY_VAR" "$API_KEY"
  chmod 600 "$DATA_DIR_RESOLVED/.env"
  env_put "$KEY_VAR" "$API_KEY"
  echo "✓ wrote $KEY_VAR to $DATA_DIR_RESOLVED/.env and .env"
else
  echo "⚠ no API key supplied for $PROVIDER — set $KEY_VAR or pass --api-key."
  echo "  The stack will start but Hermes won't be able to call the provider until"
  echo "  you add the key to $DATA_DIR_RESOLVED/.env and restart: docker restart hermes"
fi

# Telegram channel (optional) — written to the same /opt/data/.env Hermes reads.
if [ -n "$TG_TOKEN" ]; then
  dataenv_set "$DATA_DIR_RESOLVED/.env" TELEGRAM_BOT_TOKEN "$TG_TOKEN"
  dataenv_set "$DATA_DIR_RESOLVED/.env" TELEGRAM_ALLOWED_USERS "$TG_USERS"
  chmod 600 "$DATA_DIR_RESOLVED/.env" 2>/dev/null || true
  echo "✓ configured Telegram channel (bot token + allowed users) in $DATA_DIR_RESOLVED/.env"
  # On a fresh install Hermes starts fresh in the next step and reads this; on a
  # re-run it's already up, so restart it to pick up the new channel.
  if [ "$(docker inspect -f '{{.State.Running}}' hermes 2>/dev/null)" = "true" ]; then
    docker restart hermes >/dev/null 2>&1 || true
  fi
fi

# Discord channel (optional) — same /opt/data/.env Hermes reads.
if [ -n "$DISCORD_TOKEN" ]; then
  dataenv_set "$DATA_DIR_RESOLVED/.env" DISCORD_BOT_TOKEN "$DISCORD_TOKEN"
  dataenv_set "$DATA_DIR_RESOLVED/.env" DISCORD_ALLOWED_USERS "$DISCORD_USERS"
  chmod 600 "$DATA_DIR_RESOLVED/.env" 2>/dev/null || true
  echo "✓ configured Discord channel (bot token + allowed users) in $DATA_DIR_RESOLVED/.env"
  if [ "$(docker inspect -f '{{.State.Running}}' hermes 2>/dev/null)" = "true" ]; then
    docker restart hermes >/dev/null 2>&1 || true
  fi
fi

# ── 7. pull + build + up ─────────────────────────────────────────────────────
[ "$DO_PULL" -eq 1 ] && make --no-print-directory pull
[ "$DO_BUILD" -eq 1 ] && make --no-print-directory build
make --no-print-directory up
# Neutralize any stray agent-installed package overlay + PYTHONPATH drift so the
# baked venv stays authoritative (idempotent; no-op on a clean install).
make --no-print-directory hermes-heal
wait_hermes_healthy

# ── 8. set a valid default model + end-to-end probe ──────────────────────────
# The image seeds an invalid default model on OpenRouter, so set a known-good one.
docker exec hermes hermes config set model.default "$MODEL" >/dev/null
echo "✓ set model.default = $MODEL"

if [ -n "$API_KEY" ]; then
  echo "→ probing Hermes end-to-end…"
  if docker exec hermes hermes -z "Say PONG and nothing else" 2>&1 | grep -qi pong; then
    echo "✓ Hermes answered through $PROVIDER — install verified"
  else
    echo "⚠ Hermes did not return PONG. Check the model id ('$MODEL') and key."
    echo "  Re-run with --model <valid-id>, then: docker exec hermes hermes -z 'ping'"
  fi
fi

# ── 9. Hindsight memory (default on; --no-memory to skip) ─────────────────────
# The stack already runs the hindsight + hindsight_db containers, but Hermes
# doesn't use them until memory.provider is set. No pip install is needed — the
# `hindsight-client` package ships in the hermes image and registers the provider
# (Hermes also auto-installs/upgrades it on session start if missing/outdated).
# `make memory` sets the memory.* keys and restarts hermes; the HINDSIGHT_API_URL
# that docker-compose passes in lets the provider reach Hindsight.
if [ "$WITH_MEMORY" -eq 1 ]; then
  # Make sure Hindsight's extraction models are available locally first.
  pull_hindsight_models
  echo "→ enabling Hindsight as Hermes's memory provider…"
  make --no-print-directory memory
  wait_hermes_healthy
  # `hermes memory status` is the authoritative check — reports whether the
  # provider is not just configured but actually reachable.
  if docker exec hermes hermes memory status 2>/dev/null | grep -qE 'Status:[[:space:]]*available'; then
    echo "✓ Hindsight is the active memory provider and reachable"
  else
    echo "⚠ Hindsight is configured but reports 'not available'. Check:"
    echo "    docker exec hermes hermes memory status"
    echo "  (commonly HINDSIGHT_API_URL isn't reaching the hermes container)."
  fi
  # Confirm the memory backend itself is reachable from the host.
  if curl -fsS --max-time 5 "http://localhost:${HINDSIGHT_API_PORT:-8888}/health" >/dev/null 2>&1; then
    echo "✓ Hindsight API healthy (http://localhost:${HINDSIGHT_API_PORT:-8888}/health)"
  else
    echo "⚠ Hindsight API not responding yet — give it a moment, then check"
    echo "  'docker logs hindsight'."
  fi
else
  echo "→ skipping Hindsight memory wiring (--no-memory)"
fi

# ── 11. Windmill workspace + worker Python (default on; --no-windmill to skip) ─
if [ "$WITH_WINDMILL" -eq 1 ]; then
  setup_windmill
else
  echo "→ skipping Windmill setup (--no-windmill)"
fi

# ── 12. MLX host server (opt-in; Apple Silicon only) ─────────────────────────
if [ "$WITH_MLX" -eq 1 ]; then
  setup_mlx
fi

# ── 13. Headroom context-compression routing (opt-in) ────────────────────────
if [ "$WITH_HEADROOM" -eq 1 ]; then
  if [ "$PROVIDER" = "openrouter" ] && [ -n "$API_KEY" ]; then
    echo "→ routing Hermes through Headroom (context compression)…"
    make --no-print-directory headroom
  else
    echo "⚠ --with-headroom needs the openrouter provider + an API key — skipping."
  fi
fi

# ── 14. Hermes skills (data-platform pipeline-authoring skill, additive) ─────
make --no-print-directory hermes-skills-push

echo
echo "Done. Services:"
echo "  Windmill:        http://windmill.localhost"
echo "  Hermes dash:     http://hermes.localhost"
# `|| true` on both: under `set -e -o pipefail` a non-matching grep would abort
# the installer on a legacy .env that predates these keys. Same reason the echo
# is an if-block rather than a `[ -n ... ] && echo` list, which exits 1 when the
# test fails and would likewise trip set -e.
DASH_USER=$(grep -E '^HERMES_DASHBOARD_BASIC_AUTH_USERNAME=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
DASH_PASS=$(grep -E '^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
if [ -n "$DASH_USER" ]; then
  echo "                   login: $DASH_USER / $DASH_PASS  (also in .env)"
fi
echo "  Hindsight UI:    http://hindsight.localhost"
echo "  Headroom stats:  http://headroom.localhost/stats"
echo
echo "Optional next steps:  make headroom        (context compression)"
echo "                      make memory-revert   (disable Hindsight memory)"
