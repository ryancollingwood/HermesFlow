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
#    8. pull images + start the stack
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
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# ── defaults / args ──────────────────────────────────────────────────────────
PROVIDER="openrouter"
API_KEY=""
MODEL=""
DO_PULL=1
CHECK_MODEL=1
WITH_MEMORY=1
WITH_WINDMILL=1
TG_TOKEN=""
TG_USERS=""

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
# reflection. Only runs when Hindsight points at the bundled `ollama` service —
# embeddings are local (BAAI/bge-small-en-v1.5) so no embedding model is needed.
pull_hindsight_models() {
  case "${HINDSIGHT_LLM_BASE_URL:-}" in
    *ollama*) : ;;
    *) echo "→ Hindsight LLM backend is '${HINDSIGHT_LLM_BASE_URL:-unset}', not Ollama — skipping model pull"; return 0 ;;
  esac
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

# Prepare Windmill: pre-install the worker Python and ensure a 'main' workspace.
#
# Why pre-install Python: the first Python job makes `uv` download managed
# CPython into the shared worker cache. With multiple worker replicas hitting a
# brand-new script at once, they race installing into the same directory and can
# leave a corrupt half-install that uv then refuses to repair — every Python
# script then fails to deploy with "Couldn't locate the interpreter". Installing
# it once up front (single, serial) avoids that race. UV_PYTHON_INSTALL_DIR must
# match the path Windmill uses so the warmed interpreter is the one it picks up.
setup_windmill() {
  echo "→ preparing Windmill workers (pre-installing Python to avoid first-run races)…"
  if docker compose exec -T --index 1 windmill_worker \
       sh -c 'UV_PYTHON_INSTALL_DIR=/tmp/windmill/cache/py_runtime uv python install 3.12' \
       >/dev/null 2>&1; then
    echo "✓ worker Python 3.12 pre-installed into the shared cache"
  else
    echo "⚠ could not pre-install worker Python (non-fatal — the first job will try)."
  fi

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

# Verify $MODEL is offered by $PROVIDER; exit early with a helpful message if not.
validate_model() {
  [ "$CHECK_MODEL" -eq 1 ] || { echo "→ skipping model check (--skip-model-check)"; return 0; }
  [ -n "$API_KEY" ] || { echo "→ no API key — skipping model check"; return 0; }

  echo "→ validating model '$MODEL' against $PROVIDER…"
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
    --skip-model-check) CHECK_MODEL=0; shift ;;
    --no-memory) WITH_MEMORY=0; shift ;;
    --no-windmill) WITH_WINDMILL=0; shift ;;
    --telegram-bot-token) TG_TOKEN="$2"; shift 2 ;;
    --telegram-allowed-users) TG_USERS="$2"; shift 2 ;;
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

# ── 4. secrets (API key + DB passwords) ──────────────────────────────────────
make --no-print-directory secrets

# ── 5. data dirs + ownership ─────────────────────────────────────────────────
make --no-print-directory init

# Resolve DATA_DIR exactly as the recipes do (Compose-style ${HOME} expansion).
set -a; . ./.env; set +a
DATA_DIR_RESOLVED="$(eval echo "${DATA_DIR:-$HOME/.hermes}")"

# ── 6. provider key → <DATA_DIR>/.env ────────────────────────────────────────
# The compose `hermes` service leaves provider keys commented out and reads them
# from /opt/data/.env instead (what the wizard writes). We write it directly so
# no interactive wizard is needed.
if [ -n "$API_KEY" ]; then
  mkdir -p "$DATA_DIR_RESOLVED"
  if [ -f "$DATA_DIR_RESOLVED/.env" ] && grep -qE "^$KEY_VAR=" "$DATA_DIR_RESOLVED/.env"; then
    sed -i.bak -E "s|^$KEY_VAR=.*|$KEY_VAR=$API_KEY|" "$DATA_DIR_RESOLVED/.env" && rm -f "$DATA_DIR_RESOLVED/.env.bak"
  else
    printf '%s=%s\n' "$KEY_VAR" "$API_KEY" >> "$DATA_DIR_RESOLVED/.env"
  fi
  chmod 600 "$DATA_DIR_RESOLVED/.env"
  echo "✓ wrote $KEY_VAR to $DATA_DIR_RESOLVED/.env"
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

# ── 7. pull + up ─────────────────────────────────────────────────────────────
[ "$DO_PULL" -eq 1 ] && make --no-print-directory pull
make --no-print-directory up
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

echo
echo "Done. Services:"
echo "  Windmill:        http://windmill.localhost"
echo "  Hermes dash:     http://hermes.localhost"
echo "  Hindsight UI:    http://hindsight.localhost"
echo "  Headroom stats:  http://headroom.localhost/stats"
echo
echo "Optional next steps:  make headroom        (context compression)"
echo "                      make memory-revert   (disable Hindsight memory)"
