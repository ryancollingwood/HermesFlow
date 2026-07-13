# =============================================================================
#  Hermes + Windmill stack — operator Makefile
#  Run `make` (or `make help`) to see targets.
#  First time on a host:  make bootstrap
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose
HERMES_IMAGE := nousresearch/hermes-agent:latest

# Load defaults from .env if present (used for mkdir/backup paths). Values that
# reference $$HOME are expanded by the shell when the recipe sources the file.
ENV_FILE := .env

# On Windows (Git Bash / MSYS2), HOME is a Windows path (C:\Users\...).
# Docker Desktop bind mounts require POSIX paths (/c/Users/...).
# Detect and convert once so every recipe that needs a mount-safe path uses
# DOCKER_HOME instead of $$HOME.
ifeq ($(OS),Windows_NT)
  # Convert C:\Users\foo → /c/Users/foo  (works in Git Bash / MSYS2)
  DOCKER_HOME := $(shell echo "$$HOME" | sed 's|^\([A-Za-z]\):|/\L\1|; s|\\|/|g')
  ON_WINDOWS := 1
else
  DOCKER_HOME := $(HOME)
  ON_WINDOWS :=
endif

.PHONY: help check init apikey secrets wizard secure fix-permissions pull build up down restart logs ps health backup backup-schedule backup-schedule-revert bootstrap hermes-heal hermes-workspace hermes-secure hermes-skills-push hermes-skills-pull hermesflow-mcp lint validate test ci headroom headroom-revert mlx mlx-revert mlx-status memory memory-revert hindsight-mlx hindsight-mlx-revert aux-cloud aux-local aux-hindsight aux-status collection-db-migrate windmill-push windmill-pull windmill-check windmill-mcp baserow baserow-revert baserow-mcp directus directus-revert observability observability-revert ollama ollama-revert

# Fill an .env variable with a generated value when it is empty OR still set to a
# known-weak default. Usage: $(call ensure_secret,VAR,GENERATOR,WEAK_DEFAULT)
# WEAK_DEFAULT may be empty to only fill blanks.
define ensure_secret
	@CUR=$$(grep -E '^$(1)=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  if [ -z "$$CUR" ] || [ "$$CUR" = "$(3)" ]; then \
	    VAL=$$($(2)); \
	    if grep -qE '^$(1)=' $(ENV_FILE); then \
	      sed -i.bak "s|^$(1)=.*|$(1)=$$VAL|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	    else \
	      { [ -s $(ENV_FILE) ] && [ -n "$$(tail -c1 $(ENV_FILE))" ] && printf '\n' >> $(ENV_FILE); }; \
	      echo "$(1)=$$VAL" >> $(ENV_FILE); \
	    fi; \
	    echo "✓ generated $(1)"; \
	  else \
	    echo "→ $(1) already set"; \
	  fi
endef

help: ## Show this help
	@echo "Hermes + Windmill stack"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "First run on a new host:  make bootstrap"

check: ## Verify Docker + Compose are present and .env exists
	@command -v docker >/dev/null || { echo "✗ docker not found"; exit 1; }
	@$(COMPOSE) version >/dev/null 2>&1 || { echo "✗ 'docker compose' not found"; exit 1; }
	@test -f $(ENV_FILE) || { echo "✗ .env missing — run 'make init' and fill it in"; exit 1; }
	@echo "✓ prerequisites OK"

init: ## Create .env from the example and make the data directories
	@test -f $(ENV_FILE) || { cp .env.example $(ENV_FILE); echo "→ created .env from example — EDIT IT before continuing"; }
	@set -a; . ./$(ENV_FILE); set +a; \
	  DOCKER_HOME="$(DOCKER_HOME)"; \
	  mkdir -p "$${DATA_DIR:-$$DOCKER_HOME/.hermes}" \
	           "$${SHARED_DIR:-$$DOCKER_HOME/.shared_agent_data}/artifacts" \
	           "$${WM_DATA_DIR:-$$DOCKER_HOME/.windmill}"/{db,logs,cache} \
	           "$${WM_LSP_CACHE_DIR:-$$DOCKER_HOME/.windmill/lsp_cache}" \
	           "$${CADDY_DATA_DIR:-$$DOCKER_HOME/.caddy/data}" \
	           "$${CADDY_CONFIG_DIR:-$$DOCKER_HOME/.caddy/config}"; \
	  echo "✓ data directories created"
	@$(MAKE) --no-print-directory fix-permissions

apikey: ## Generate API_SERVER_KEY in .env if it's empty
	@grep -q '^API_SERVER_KEY=.\+' $(ENV_FILE) 2>/dev/null && { echo "→ API_SERVER_KEY already set"; exit 0; } || true
	@KEY=$$(openssl rand -hex 32); \
	  if grep -q '^API_SERVER_KEY=' $(ENV_FILE); then \
	    sed -i.bak "s|^API_SERVER_KEY=.*|API_SERVER_KEY=$$KEY|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else \
	    echo "API_SERVER_KEY=$$KEY" >> $(ENV_FILE); \
	  fi; \
	  echo "✓ generated API_SERVER_KEY"

secrets: ## Generate every required secret in .env that's blank or still a weak default
	$(call ensure_secret,API_SERVER_KEY,openssl rand -hex 32,)
	$(call ensure_secret,WM_DB_PASSWORD,openssl rand -hex 32,windmill)
	$(call ensure_secret,HINDSIGHT_DB_PASSWORD,openssl rand -hex 16,hindsight)
	$(call ensure_secret,GRAFANA_ADMIN_PASSWORD,openssl rand -hex 16,changeme)
	$(call ensure_secret,COLLECTION_DB_ADMIN_PASSWORD,openssl rand -hex 16,collection)
	$(call ensure_secret,BASEROW_SECRET_KEY,openssl rand -hex 32,)
	$(call ensure_secret,BASEROW_DB_PASSWORD,openssl rand -hex 16,)
	$(call ensure_secret,DIRECTUS_DB_PASSWORD,openssl rand -hex 16,)
	$(call ensure_secret,WINDMILL_COLLECTION_DB_PASSWORD,openssl rand -hex 16,)
	$(call ensure_secret,DIRECTUS_KEY,openssl rand -hex 16,)
	$(call ensure_secret,DIRECTUS_SECRET,openssl rand -hex 32,)
	$(call ensure_secret,DIRECTUS_ADMIN_PASSWORD,openssl rand -hex 16,)
	$(call ensure_secret,BASEROW_REDIS_PASSWORD,openssl rand -hex 16,)

wizard: ## Run the Hermes first-run setup wizard (interactive; writes ~/.hermes/.env + config)
	@set -a; . ./$(ENV_FILE); set +a; \
	  HERMES_DATA="$${DATA_DIR:-$(DOCKER_HOME)/.hermes}"; \
	  docker run -it --rm \
	    -v "$$HERMES_DATA":/opt/data \
	    $(HERMES_IMAGE) setup

secure: ## chmod 600 the secret files (skipped on Windows where chmod is a no-op)
ifdef ON_WINDOWS
	@echo "→ Windows host detected — skipping chmod (NTFS ACLs control access; ensure only your user owns these files)"
else
	@chmod 600 $(ENV_FILE) 2>/dev/null && echo "✓ chmod 600 .env" || true
	@set -a; . ./$(ENV_FILE); set +a; \
	  HERMES_DATA="$${DATA_DIR:-$$HOME/.hermes}"; \
	  test -f "$$HERMES_DATA/.env" && chmod 600 "$$HERMES_DATA/.env" && echo "✓ chmod 600 $$HERMES_DATA/.env" || true
endif

fix-permissions: ## Chown bind-mount dirs to HERMES_UID:HERMES_GID so containers can write to them
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  UID_VAL="$${HERMES_UID:-1000}"; \
	  GID_VAL="$${HERMES_GID:-1000}"; \
	  DOCKER_HOME="$(DOCKER_HOME)"; \
	  DATA="$${DATA_DIR:-$$DOCKER_HOME/.hermes}"; \
	  SHARED="$${SHARED_DIR:-$$DOCKER_HOME/.shared_agent_data}"; \
	  WM="$${WM_DATA_DIR:-$$DOCKER_HOME/.windmill}"; \
	  WM_LSP="$${WM_LSP_CACHE_DIR:-$$DOCKER_HOME/.windmill/lsp_cache}"; \
	  CADDY_D="$${CADDY_DATA_DIR:-$$DOCKER_HOME/.caddy/data}"; \
	  CADDY_C="$${CADDY_CONFIG_DIR:-$$DOCKER_HOME/.caddy/config}"; \
	  echo "→ fixing ownership to $$UID_VAL:$$GID_VAL on bind-mount directories…"; \
	  docker run --rm \
	    -v "$$DATA":/mnt/data \
	    -v "$$SHARED":/mnt/shared \
	    -v "$$WM":/mnt/wm \
	    -v "$$WM_LSP":/mnt/wm_lsp \
	    -v "$$CADDY_D":/mnt/caddy_data \
	    -v "$$CADDY_C":/mnt/caddy_config \
	    alpine:3 \
	    sh -c "chown -R $$UID_VAL:$$GID_VAL /mnt/data /mnt/shared /mnt/wm /mnt/wm_lsp /mnt/caddy_data /mnt/caddy_config && chmod -R u+rwX /mnt/data /mnt/shared /mnt/wm /mnt/wm_lsp /mnt/caddy_data /mnt/caddy_config"; \
	  echo "✓ ownership corrected"

pull: ## Pull the latest images (skips the locally-built Hermes image)
	@$(COMPOSE) pull --ignore-buildable

build: ## Build the local Hermes image (bakes in hermes/requirements.txt packages)
	@$(COMPOSE) build hermes

hermes-heal: ## Remove stray agent-installed package overlay + PYTHONPATH drift (idempotent)
	@docker ps --format '{{.Names}}' | grep -qx hermes || { \
	  echo "✗ hermes not running — 'make up' first"; exit 1; }
	@changed=0; \
	if docker exec hermes test -e /opt/data/.hermes-extras 2>/dev/null; then \
	  echo "→ removing redundant overlay /opt/data/.hermes-extras (firecrawl is in the venv)"; \
	  docker exec hermes rm -rf /opt/data/.hermes-extras; changed=1; fi; \
	if docker exec hermes sh -c 'grep -qE "^PYTHONPATH=.*hermes-extras" /opt/data/.env 2>/dev/null'; then \
	  echo "→ stripping PYTHONPATH drift from /opt/data/.env (backup: .env.bak)"; \
	  docker exec hermes sh -c 'cp /opt/data/.env /opt/data/.env.bak && grep -vE "^PYTHONPATH=.*hermes-extras" /opt/data/.env.bak > /opt/data/.env'; changed=1; fi; \
	if [ "$$changed" = 1 ]; then \
	  echo "→ restarting hermes to apply"; $(COMPOSE) restart hermes >/dev/null; \
	  echo "✓ drift neutralized — venv is authoritative"; \
	else echo "✓ no overlay drift (venv is authoritative)"; fi

hermes-workspace: ## Point Hermes's gateway/cron working dir at /shared (instead of /opt/hermes)
	@docker exec hermes hermes config set terminal.cwd /shared
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes gateway/cron jobs now write to /shared (host: $${SHARED_DIR:-./data/shared})"

hermes-secure: ## Fix Tirith PATH lookup (was failing open, unscanned) + disable allow_lazy_installs drift (idempotent)
	@docker ps --format '{{.Names}}' | grep -qx hermes || { \
	  echo "✗ hermes not running — 'make up' first"; exit 1; }
	@changed=0; \
	if ! docker exec hermes grep -qE '^\s*tirith_path:\s*/opt/data/bin/tirith\s*$$' /opt/data/config.yaml 2>/dev/null; then \
	  echo "→ pointing tirith_path at /opt/data/bin/tirith (bare 'tirith' isn't on \$$PATH, so scans were silently skipped under tirith_fail_open)"; \
	  docker exec hermes hermes config set security.tirith_path /opt/data/bin/tirith >/dev/null; changed=1; fi; \
	if docker exec hermes grep -qE '^\s*allow_lazy_installs:\s*[Tt]rue\s*$$' /opt/data/config.yaml 2>/dev/null; then \
	  echo "→ disabling security.allow_lazy_installs (README says never re-enable this; venv is read-only anyway, but the app shouldn't even attempt it)"; \
	  docker exec hermes hermes config set security.allow_lazy_installs false >/dev/null; changed=1; fi; \
	if [ "$$changed" = 1 ]; then \
	  $(COMPOSE) restart hermes >/dev/null; \
	  echo "✓ Tirith now active on \$$PATH-resolved binary; lazy installs disabled"; \
	else echo "✓ already secure (tirith_path correct, allow_lazy_installs false)"; fi

hermes-skills-push: ## Copy hermes/skills/ into the Hermes-bound DATA_DIR/skills/ (additive — never deletes skills not tracked here)
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  dest="$${DATA_DIR:-$$HOME/.hermes/data}/skills"; \
	  if [ ! -d hermes/skills ]; then echo "→ no hermes/skills/ in this repo — nothing to push"; exit 0; fi; \
	  mkdir -p "$$dest"; \
	  cp -R hermes/skills/. "$$dest/"; \
	  echo "✓ copied hermes/skills/ → $$dest"; \
	  echo "  (additive: any skill already under $$dest but not in this repo is left untouched)"

hermes-skills-pull: ## Pull tracked skills FROM the Hermes-bound DATA_DIR/skills/ back into hermes/skills/ for review — scoped to skills already tracked here, never imports Hermes's bundled/curated skills
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  src="$${DATA_DIR:-$$HOME/.hermes/data}/skills"; \
	  if [ ! -d hermes/skills ]; then echo "→ no hermes/skills/ in this repo yet — nothing to scope the pull to"; exit 0; fi; \
	  if [ ! -d "$$src" ]; then echo "✗ $$src not found"; exit 1; fi; \
	  found=0; \
	  for d in $$(find hermes/skills -mindepth 2 -maxdepth 2 -type d); do \
	    rel="$${d#hermes/skills/}"; \
	    if [ -d "$$src/$$rel" ]; then \
	      cp -R "$$src/$$rel/." "$$d/"; \
	      echo "→ pulled $$rel"; found=1; \
	    fi; \
	  done; \
	  if [ "$$found" = 1 ]; then echo "✓ pulled tracked skills from $$src — review 'git diff' before committing"; \
	  else echo "→ none of the tracked skills under hermes/skills/ exist in $$src"; fi

hermesflow-mcp: hermes-skills-push ## Register the narrow HF-028 product-collection execution tool with Hermes
	@if ! docker inspect -f '{{.State.Running}}' hermes 2>/dev/null | grep -qx true; then \
	  echo "✗ Hermes container is not running — run 'make up' first"; exit 1; fi
	@docker exec hermes test -f /opt/data/skills/workflow-orchestration/hermesflow/scripts/product_collection_mcp.py
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  ENVF="$(ENV_FILE)"; \
	  envput() { if grep -qE "^$$1=" "$$ENVF"; then sed -i.bak "s|^$$1=.*|$$1=$$2|" "$$ENVF" && rm -f "$$ENVF.bak"; else { [ -s "$$ENVF" ] && [ -n "$$(tail -c1 "$$ENVF")" ] && printf '\n' >> "$$ENVF"; }; printf '%s=%s\n' "$$1" "$$2" >> "$$ENVF"; fi; }; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  token="$${HF_PRODUCT_MCP_TOKEN:-}"; \
	  if [ -n "$$token" ] && curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" "$$base/api/w/main/jobs/list?per_page=1" >/dev/null 2>&1; then \
	    echo "→ reusing dedicated HF_PRODUCT_MCP_TOKEN from $$ENVF"; \
	  else \
	    admin=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	      -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	    [ -n "$$admin" ] || { echo "✗ couldn't mint the dedicated HermesFlow token — set HF_PRODUCT_MCP_TOKEN in $$ENVF"; exit 1; }; \
	    token=$$(curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$admin" -H 'Content-Type: application/json' \
	      -X POST "$$base/api/users/tokens/create" \
	      -d '{"label":"hermesflow-product-collection","scopes":["jobs:run","jobs:read"]}' 2>/dev/null | tr -d '"'); \
	    [ -n "$$token" ] || { echo "✗ dedicated HermesFlow token creation failed"; exit 1; }; \
	    envput HF_PRODUCT_MCP_TOKEN "$$token"; \
	    echo "→ minted dedicated Windmill token (jobs:run + jobs:read; available only to the fixed-flow MCP server)"; \
	  fi; \
	  docker exec hermes hermes mcp remove hermesflow >/dev/null 2>&1 || true; \
	  echo y | docker exec -i hermes hermes mcp add hermesflow \
	    --command /opt/hermes/.venv/bin/python \
	    --env "WINDMILL_MCP_TOKEN=$$token" \
	    --args /opt/data/skills/workflow-orchestration/hermesflow/scripts/product_collection_mcp.py >/dev/null; \
	  docker exec hermes hermes mcp test hermesflow 2>&1 | grep -qi "Tools discovered" \
	    || { echo "✗ HermesFlow MCP registration could not be verified"; exit 1; }
	@echo "✓ registered HermesFlow product-collection MCP server (stdio, one bounded execution tool)"

up: ## Start the stack (detached)
	@$(COMPOSE) up -d
	@echo "→ Windmill:  http://windmill.localhost   Hermes dash: http://hermes.localhost"

down: ## Stop the stack (volumes preserved)
	@$(COMPOSE) down

restart: ## Recreate the stack with current images
	@$(COMPOSE) up -d --force-recreate

logs: ## Follow logs for all services (Ctrl-C to stop)
	@$(COMPOSE) logs -f --tail=100

ps: ## Show container status
	@$(COMPOSE) ps

health: ## Probe Hermes /health and Windmill /api/version
	@set -a; . ./$(ENV_FILE); set +a; \
	  echo -n "Hermes:   "; curl -fsS "http://127.0.0.1:$${API_SERVER_PORT:-8642}/health" || echo "unreachable"; echo; \
	  echo -n "Windmill: "; curl -fsS -H "Host: windmill.localhost" "http://127.0.0.1:$${CADDY_HTTP_PORT:-80}/api/version" || echo "unreachable"; echo

headroom: ## Route Hermes through the Headroom context-compression proxy
	@docker exec hermes hermes config set model.provider custom
	@docker exec hermes hermes config set model.base_url http://headroom:8787/v1
	@docker exec hermes sh -c 'set -a; . /opt/data/.env; set +a; hermes config set model.api_key "$$OPENROUTER_API_KEY"' >/dev/null
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes is routing through Headroom"
	@echo "  Stats:     http://headroom.localhost/stats  (or http://localhost:8787/stats)"
	@echo "  Dashboard: http://headroom.localhost/dashboard"
	@echo "  Metrics:   http://headroom.localhost/metrics"

headroom-revert: ## Revert Hermes to direct provider routing (bypass Headroom)
	@docker exec hermes hermes config set model.base_url ""
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes is now routing directly to the provider"

baserow: ## Start Baserow (structured-data UI + REST API); adds the override to COMPOSE_FILE
	@$(MAKE) --no-print-directory secrets
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  case ":$$CUR:" in \
	    *:docker-compose.baserow.yml:*) NEW="$$CUR" ;; \
	    *) if [ -z "$$CUR" ]; then NEW="docker-compose.yml:docker-compose.baserow.yml"; \
	       else NEW="$$CUR:docker-compose.baserow.yml"; fi ;; \
	  esac; \
	  if grep -qE '^COMPOSE_FILE=' $(ENV_FILE); then \
	    sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else \
	    echo "COMPOSE_FILE=$$NEW" >> $(ENV_FILE); \
	  fi; \
	  echo "✓ COMPOSE_FILE=$$NEW"
	@$(COMPOSE) up -d collection_db baserow_redis baserow
	@echo "✓ Baserow is starting (first boot runs DB migrations — give it a minute)"
	@echo "  UI / API: http://baserow.localhost   (direct: http://localhost:$${BASEROW_PORT:-3010})"

baserow-revert: ## Stop Baserow + drop its override from COMPOSE_FILE (volumes/data preserved)
	@$(COMPOSE) rm -sf baserow baserow_redis 2>/dev/null || true
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  NEW=$$(echo "$$CUR" | sed -E 's|:?docker-compose\.baserow\.yml||'); \
	  if [ -z "$$NEW" ] || [ "$$NEW" = "docker-compose.yml" ]; then NEW=""; fi; \
	  sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  echo "✓ COMPOSE_FILE=$${NEW:-(cleared)}"
	@echo "✓ Baserow stopped (data preserved in volumes)"

baserow-mcp: ## Register Baserow with Hermes as MCP tools (creates the endpoint + wires mcp-remote)
	@command -v python3 >/dev/null || { echo "✗ python3 required"; exit 1; }
	@docker ps --format '{{.Names}}' | grep -qx baserow || { echo "✗ Baserow isn't running — run 'make baserow' first"; exit 1; }
	@docker exec hermes sh -c 'command -v mcp-remote >/dev/null' 2>/dev/null || { echo "✗ the running Hermes image lacks mcp-remote — rebuild it: make build && make up"; exit 1; }
	@set -a; . ./$(ENV_FILE); set +a; \
	  BURL="http://localhost:$${BASEROW_PORT:-3010}"; ENVF="$(ENV_FILE)"; \
	  envput() { if grep -qE "^$$1=" "$$ENVF"; then sed -i.bak "s|^$$1=.*|$$1=$$2|" "$$ENVF" && rm -f "$$ENVF.bak"; else { [ -s "$$ENVF" ] && [ -n "$$(tail -c1 "$$ENVF")" ] && printf '\n' >> "$$ENVF"; }; printf '%s=%s\n' "$$1" "$$2" >> "$$ENVF"; fi; }; \
	  EMAIL="$${BASEROW_EMAIL}"; PASS="$${BASEROW_PASSWORD}"; GEN=0; \
	  [ -n "$$EMAIL" ] || { EMAIL="hermes@baserow.local"; GEN=1; }; \
	  [ -n "$$PASS" ] || { PASS="$$(openssl rand -hex 16)"; GEN=1; }; \
	  TOKEN=$$(curl -fsS -X POST "$$BURL/api/user/token-auth/" -H 'Content-Type: application/json' \
	      -d "{\"email\":\"$$EMAIL\",\"password\":\"$$PASS\"}" 2>/dev/null \
	    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("access_token") or d.get("token") or "")' 2>/dev/null); \
	  if [ -z "$$TOKEN" ]; then \
	    echo "→ no existing login for $$EMAIL — creating the account…"; \
	    NAME=$$(printf '%s' "$$EMAIL" | cut -d@ -f1); [ "$${#NAME}" -ge 2 ] || NAME="Baserow User"; \
	    REG=$$(curl -sS -X POST "$$BURL/api/user/" -H 'Content-Type: application/json' \
	      -d "{\"name\":\"$$NAME\",\"email\":\"$$EMAIL\",\"password\":\"$$PASS\",\"language\":\"en\",\"authenticate\":true}" 2>/dev/null); \
	    TOKEN=$$(printf '%s' "$$REG" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("access_token") or d.get("token") or "")' 2>/dev/null); \
	    if [ -n "$$TOKEN" ]; then echo "✓ created Baserow account $$EMAIL"; \
	    else \
	      ERR=$$(printf '%s' "$$REG" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("error") or d.get("detail") or "unknown error")' 2>/dev/null); \
	      echo "✗ couldn't log in or create the account ($${ERR:-unreachable})."; \
	      echo "  • If the account already exists, the password didn't match — fix BASEROW_PASSWORD (or your input)."; \
	      echo "  • If signups are disabled, create it in the UI: http://baserow.localhost"; \
	      echo "  See docs/baserow-accounts-and-sharing.md for account + data-sharing options."; \
	      exit 1; \
	    fi; \
	  fi; \
	  if [ "$$GEN" = "1" ]; then envput BASEROW_EMAIL "$$EMAIL"; envput BASEROW_PASSWORD "$$PASS"; echo "→ saved BASEROW_EMAIL / BASEROW_PASSWORD to $$ENVF"; fi; \
	  WSID="$${BASEROW_WORKSPACE_ID}"; \
	  PRESENT=$$(curl -fsS "$$BURL/api/workspaces/" -H "Authorization: JWT $$TOKEN" \
	    | WSWANT="$$WSID" python3 -c 'import sys,os,json; w=os.environ.get("WSWANT",""); print(w if w and any(str(x["id"])==w for x in json.load(sys.stdin)) else "")'); \
	  if [ -n "$$PRESENT" ]; then echo "→ using existing workspace id $$WSID"; \
	  else \
	    WSID=$$(curl -fsS -X POST "$$BURL/api/workspaces/" -H "Authorization: JWT $$TOKEN" -H 'Content-Type: application/json' -d "{\"name\":\"$${BASEROW_MCP_WORKSPACE:-Hermes}\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'); \
	    envput BASEROW_WORKSPACE_ID "$$WSID"; \
	    echo "→ created workspace (id $$WSID) — saved BASEROW_WORKSPACE_ID to $$ENVF"; \
	  fi; \
	  KEY=$$(curl -fsS "$$BURL/api/mcp/endpoints/" -H "Authorization: JWT $$TOKEN" \
	    | WSID="$$WSID" python3 -c 'import sys,os,json; w=os.environ["WSID"]; e=[x for x in json.load(sys.stdin) if x["name"]=="hermes" and str(x.get("workspace_id"))==w]; print(e[0]["key"] if e else "")'); \
	  if [ -z "$$KEY" ]; then \
	    KEY=$$(curl -fsS -X POST "$$BURL/api/mcp/endpoints/" -H "Authorization: JWT $$TOKEN" -H 'Content-Type: application/json' -d "{\"name\":\"hermes\",\"workspace_id\":$$WSID}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])'); \
	    echo "→ created MCP endpoint 'hermes'"; \
	  else echo "→ reusing existing MCP endpoint 'hermes'"; fi; \
	  [ -n "$$KEY" ] || { echo "✗ could not obtain the MCP endpoint key"; exit 1; }; \
	  docker exec hermes hermes mcp remove baserow >/dev/null 2>&1 || true; \
	  printf 'Y\n' | docker exec -i hermes hermes mcp add baserow --command /usr/local/bin/mcp-remote \
	    --args "http://baserow/mcp/$$KEY/sse" --allow-http --transport sse-only >/dev/null 2>&1; \
	  echo "→ verifying connection…"; \
	  docker exec hermes hermes mcp test baserow 2>&1 | grep -iE "Connected|Tools discovered" \
	    || { echo "⚠ couldn't confirm — check: docker exec hermes hermes mcp test baserow"; exit 0; }; \
	  echo "✓ Baserow MCP tools are available to Hermes (start a new session to use them)"

directus: ## Start Directus (triage UI + REST/GraphQL API + MCP); adds the override to COMPOSE_FILE
	@$(MAKE) --no-print-directory secrets
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  case ":$$CUR:" in \
	    *:docker-compose.directus.yml:*) NEW="$$CUR" ;; \
	    *) if [ -z "$$CUR" ]; then NEW="docker-compose.yml:docker-compose.directus.yml"; \
	       else NEW="$$CUR:docker-compose.directus.yml"; fi ;; \
	  esac; \
	  if grep -qE '^COMPOSE_FILE=' $(ENV_FILE); then \
	    sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else \
	    echo "COMPOSE_FILE=$$NEW" >> $(ENV_FILE); \
	  fi; \
	  echo "✓ COMPOSE_FILE=$$NEW"
	@$(COMPOSE) up -d collection_db directus
	@echo "✓ Directus is starting (first boot runs DB migrations — give it a minute)"
	@echo "  UI / API: http://directus.localhost   (direct: http://localhost:$${DIRECTUS_PORT:-8055})"
	@echo "  Log in with DIRECTUS_ADMIN_EMAIL / DIRECTUS_ADMIN_PASSWORD from .env"
	@echo "  → To give Hermes MCP access: open the UI, Settings → AI, enable MCP, generate"
	@echo "    an access token for a dedicated MCP user, then register that endpoint with"
	@echo "    Hermes (MCP is a Studio-only setting in Directus — not scriptable here, unlike"
	@echo "    'make baserow-mcp')."

directus-revert: ## Stop Directus + drop its override from COMPOSE_FILE (volumes/data preserved)
	@$(COMPOSE) rm -sf directus 2>/dev/null || true
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  NEW=$$(echo "$$CUR" | sed -E 's|:?docker-compose\.directus\.yml||'); \
	  if [ -z "$$NEW" ] || [ "$$NEW" = "docker-compose.yml" ]; then NEW=""; fi; \
	  sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  echo "✓ COMPOSE_FILE=$${NEW:-(cleared)}"
	@echo "✓ Directus stopped (data preserved; collection_db keeps running for Baserow/Windmill)"

ollama: ## Start Ollama (local LLM inference); adds the override to COMPOSE_FILE
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  case ":$$CUR:" in \
	    *:docker-compose.ollama.yml:*) NEW="$$CUR" ;; \
	    *) if [ -z "$$CUR" ]; then NEW="docker-compose.yml:docker-compose.ollama.yml"; \
	       else NEW="$$CUR:docker-compose.ollama.yml"; fi ;; \
	  esac; \
	  if grep -qE '^COMPOSE_FILE=' $(ENV_FILE); then \
	    sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else \
	    echo "COMPOSE_FILE=$$NEW" >> $(ENV_FILE); \
	  fi; \
	  echo "✓ COMPOSE_FILE=$$NEW"
	@for kv in "HINDSIGHT_LLM_BASE_URL=http://ollama:11434/v1" "BASEROW_OLLAMA_HOST=http://ollama:11434"; do \
	  k="$${kv%%=*}"; v="$${kv#*=}"; \
	  if grep -qE "^$$k=" $(ENV_FILE); then sed -i.bak "s|^$$k=.*|$$k=$$v|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else echo "$$kv" >> $(ENV_FILE); fi; \
	done
	@$(COMPOSE) up -d ollama
	@echo "✓ Ollama is starting — http://ollama.localhost (direct: http://localhost:$${OLLAMA_PORT:-11434})"
	@echo "  Pull a model: docker exec ollama ollama pull llama3.2"
	@echo "  Pointed Hindsight/Baserow at the bundled container (HINDSIGHT_LLM_BASE_URL, BASEROW_OLLAMA_HOST)"

ollama-revert: ## Stop Ollama + drop its override from COMPOSE_FILE (models in OLLAMA_DATA_DIR preserved)
	@$(COMPOSE) rm -sf ollama 2>/dev/null || true
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  NEW=$$(echo "$$CUR" | sed -E 's|:?docker-compose\.ollama\.yml||'); \
	  if [ -z "$$NEW" ] || [ "$$NEW" = "docker-compose.yml" ]; then NEW=""; fi; \
	  sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  echo "✓ COMPOSE_FILE=$${NEW:-(cleared)}"
	@echo "✓ Ollama stopped (models preserved in OLLAMA_DATA_DIR)"
	@echo "  Note: docker-compose.gpu.yml patches this service — drop it from COMPOSE_FILE too if it was enabled."

observability: ## Start observability (Prometheus, Grafana, exporters, Loki/Promtail); adds the override to COMPOSE_FILE
	@$(MAKE) --no-print-directory secrets
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  case ":$$CUR:" in \
	    *:docker-compose.observability.yml:*) NEW="$$CUR" ;; \
	    *) if [ -z "$$CUR" ]; then NEW="docker-compose.yml:docker-compose.observability.yml"; \
	       else NEW="$$CUR:docker-compose.observability.yml"; fi ;; \
	  esac; \
	  if grep -qE '^COMPOSE_FILE=' $(ENV_FILE); then \
	    sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else \
	    echo "COMPOSE_FILE=$$NEW" >> $(ENV_FILE); \
	  fi; \
	  echo "✓ COMPOSE_FILE=$$NEW"
	@$(COMPOSE) up -d prometheus grafana cadvisor node_exporter postgres_exporter collection_postgres_exporter hindsight_postgres_exporter alertmanager loki promtail
	@$(COMPOSE) up -d caddy
	@echo "✓ Observability is starting"
	@echo "  Grafana:      http://grafana.localhost   (admin / see GRAFANA_ADMIN_PASSWORD)"
	@echo "  Prometheus:   http://prometheus.localhost"
	@echo "  Alertmanager: http://alertmanager.localhost"

observability-revert: ## Stop observability + drop its override from COMPOSE_FILE (volumes/data preserved)
	@$(COMPOSE) rm -sf prometheus grafana cadvisor node_exporter postgres_exporter collection_postgres_exporter hindsight_postgres_exporter alertmanager loki promtail 2>/dev/null || true
	@CUR=$$(grep -E '^COMPOSE_FILE=' $(ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2- | xargs); \
	  NEW=$$(echo "$$CUR" | sed -E 's|:?docker-compose\.observability\.yml||'); \
	  if [ -z "$$NEW" ] || [ "$$NEW" = "docker-compose.yml" ]; then NEW=""; fi; \
	  sed -i.bak "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$$NEW|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  echo "✓ COMPOSE_FILE=$${NEW:-(cleared)}"
	@$(COMPOSE) up -d caddy 2>/dev/null || true
	@echo "✓ Observability stopped (data preserved in volumes)"

mlx: ## Route Hermes to a host-native MLX server (Apple Silicon — see docs/mlx.md)
	@set -a; . ./$(ENV_FILE); set +a; \
	  docker exec hermes hermes config set model.provider custom; \
	  docker exec hermes hermes config set model.base_url "$${MLX_BASE_URL:-http://host.docker.internal:8080/v1}"; \
	  docker exec hermes hermes config set model.api_key mlx
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes is routing to the host MLX server"
	@echo "  Make sure mlx_lm.server is running on the host first — see docs/mlx.md"

mlx-revert: headroom-revert ## Revert Hermes to direct provider routing (alias of headroom-revert)

mlx-status: ## Show host MLX install (path, version, model) and test the endpoint
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  VENV="$${MLX_VENV_DIR:-$$HOME/.mlx-venv}"; \
	  PORT="$${MLX_HOST_PORT:-8080}"; \
	  MODEL="$${MLX_MODEL:-mlx-community/Qwen2.5-7B-Instruct-4bit}"; \
	  echo "MLX install:"; \
	  echo "  venv:      $$VENV"; \
	  if [ -x "$$VENV/bin/mlx_lm.server" ]; then \
	    VER=$$("$$VENV/bin/pip" show mlx-lm 2>/dev/null | awk '/^Version:/{print $$2}'); \
	    echo "  mlx-lm:    $${VER:-installed}"; \
	    echo "  server:    $$VENV/bin/mlx_lm.server"; \
	  else \
	    echo "  mlx-lm:    not installed (run ./install.sh --with-mlx, or pip install mlx-lm)"; \
	  fi; \
	  echo "  model:     $$MODEL"; \
	  echo "  endpoint:  http://localhost:$$PORT/v1"; \
	  if command -v launchctl >/dev/null 2>&1; then \
	    launchctl print "gui/$$(id -u)/com.hermesflow.mlx" >/dev/null 2>&1 \
	      && echo "  launchd:   loaded (com.hermesflow.mlx)" \
	      || echo "  launchd:   not loaded (manual start: ./mlx/serve.sh)"; \
	  fi; \
	  echo; echo "→ testing http://localhost:$$PORT …"; \
	  if ! curl -fsS -m 5 "http://localhost:$$PORT/v1/models" >/dev/null 2>&1; then \
	    echo "✗ no response on :$$PORT — start the server (make mlx / ./mlx/serve.sh)"; exit 1; \
	  fi; \
	  echo "✓ /v1/models reachable:"; \
	  curl -fsS -m 5 "http://localhost:$$PORT/v1/models" \
	    | python3 -c 'import sys,json; [print("    -",m["id"]) for m in json.load(sys.stdin).get("data",[])]' 2>/dev/null || true; \
	  echo "→ chat completion test (may load the model on first call)…"; \
	  RESP=$$(curl -fsS -m 120 "http://localhost:$$PORT/v1/chat/completions" -H 'Content-Type: application/json' \
	    -d "{\"model\":\"$$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with just: PONG\"}],\"max_tokens\":16}" 2>/dev/null); \
	  echo "$$RESP" | python3 -c 'import sys,json; print("✓ reply:", json.load(sys.stdin)["choices"][0]["message"]["content"].strip())' 2>/dev/null \
	    || { echo "✗ chat completion failed:"; echo "$$RESP" | head -c 300; echo; exit 1; }

memory: ## Enable Hindsight as Hermes's memory provider (see README "Hindsight memory")
	@docker exec hermes hermes config set memory.memory_enabled true
	@docker exec hermes hermes config set memory.provider hindsight
	@docker exec hermes hermes config set memory.user_profile_enabled true
	@docker exec hermes hermes config set memory.write_approval false
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes memory is routed through Hindsight"
	@echo "  UI:  http://hindsight.localhost (or http://localhost:9999)"
	@echo "  API: http://localhost:8888"

memory-revert: ## Disable the Hindsight memory provider
	@docker exec hermes hermes config set memory.memory_enabled false
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes memory disabled"

hindsight-mlx: ## Point Hindsight memory extraction at the host MLX server (recreates hindsight)
	@set -a; . ./$(ENV_FILE); set +a; \
	  URL="$${MLX_BASE_URL:-http://host.docker.internal:8080/v1}"; \
	  MDL="$${MLX_MODEL:-mlx-community/Qwen2.5-7B-Instruct-4bit}"; \
	  for kv in "HINDSIGHT_LLM_BASE_URL=$$URL" "HINDSIGHT_LLM_API_KEY=mlx" \
	            "HINDSIGHT_LLM_MODEL=$$MDL" "HINDSIGHT_RETAIN_LLM_MODEL=$$MDL" \
	            "HINDSIGHT_CONSOLIDATION_LLM_MODEL=$$MDL" "HINDSIGHT_REFLECT_LLM_MODEL=$$MDL"; do \
	    k="$${kv%%=*}"; v="$${kv#*=}"; \
	    if grep -qE "^$$k=" $(ENV_FILE); then sed -i.bak "s|^$$k=.*|$$k=$$v|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	    else echo "$$kv" >> $(ENV_FILE); fi; \
	  done; \
	  echo "✓ Hindsight LLM → $$URL ($$MDL)"
	@$(COMPOSE) up -d hindsight
	@echo "  Make sure mlx_lm.server is running on the host (make mlx / ./mlx/serve.sh)."

hindsight-mlx-revert: ## Revert Hindsight memory extraction to the bundled Ollama
	@for kv in "HINDSIGHT_LLM_BASE_URL=http://ollama:11434/v1" "HINDSIGHT_LLM_API_KEY=openai" \
	           "HINDSIGHT_LLM_MODEL=qwen2.5:14b" "HINDSIGHT_RETAIN_LLM_MODEL=qwen2.5:3b" \
	           "HINDSIGHT_CONSOLIDATION_LLM_MODEL=qwen2.5:14b" "HINDSIGHT_REFLECT_LLM_MODEL=qwen2.5:14b"; do \
	    k="$${kv%%=*}"; v="$${kv#*=}"; \
	    if grep -qE "^$$k=" $(ENV_FILE); then sed -i.bak "s|^$$k=.*|$$k=$$v|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	    else echo "$$kv" >> $(ENV_FILE); fi; \
	  done
	@$(COMPOSE) up -d hindsight
	@echo "✓ Hindsight memory extraction reverted to the bundled Ollama"
	@echo "  Ollama is opt-in — if it isn't already running, enable it first: make ollama"

aux-cloud: ## Pin vision/compression/web_extract/triage_specifier to cloud models, independent of main model
	@docker exec hermes hermes config set auxiliary.vision.provider openrouter
	@docker exec hermes hermes config set auxiliary.vision.model openai/gpt-4o-mini
	@docker exec hermes hermes config set auxiliary.compression.provider openrouter
	@docker exec hermes hermes config set auxiliary.compression.model google/gemini-2.5-flash
	@docker exec hermes hermes config set auxiliary.web_extract.provider openrouter
	@docker exec hermes hermes config set auxiliary.web_extract.model google/gemini-2.5-flash
	@docker exec hermes hermes config set auxiliary.triage_specifier.provider openrouter
	@docker exec hermes hermes config set auxiliary.triage_specifier.model openai/gpt-4o-mini
	@$(COMPOSE) restart hermes
	@echo "✓ vision/compression/web_extract/triage_specifier pinned to cloud, regardless of main model (mlx/headroom)"

aux-local: ## Route low-stakes auxiliary tasks (approval/title_generation/tts_audio_tags/skills_hub/mcp) to whatever main model is active
	@docker exec hermes hermes config set auxiliary.approval.provider main
	@docker exec hermes hermes config set auxiliary.title_generation.provider main
	@docker exec hermes hermes config set auxiliary.tts_audio_tags.provider main
	@docker exec hermes hermes config set auxiliary.skills_hub.provider main
	@docker exec hermes hermes config set auxiliary.mcp.provider main
	@$(COMPOSE) restart hermes
	@echo "✓ approval/title_generation/tts_audio_tags/skills_hub/mcp routed to the main model (use with 'make mlx')"

aux-hindsight: ## Route low-stakes auxiliary tasks to Hindsight's currently-configured LLM backend
	@set -a; . ./$(ENV_FILE); set +a; \
	  URL="$${HINDSIGHT_LLM_BASE_URL:-http://ollama:11434/v1}"; \
	  KEY="$${HINDSIGHT_LLM_API_KEY:-openai}"; \
	  MDL="$${HINDSIGHT_LLM_MODEL:-qwen2.5:14b}"; \
	  for t in approval title_generation tts_audio_tags skills_hub mcp; do \
	    docker exec hermes hermes config set auxiliary.$$t.provider custom; \
	    docker exec hermes hermes config set auxiliary.$$t.base_url "$$URL"; \
	    docker exec hermes hermes config set auxiliary.$$t.api_key "$$KEY"; \
	    docker exec hermes hermes config set auxiliary.$$t.model "$$MDL"; \
	  done; \
	  echo "✓ approval/title_generation/tts_audio_tags/skills_hub/mcp → $$URL ($$MDL, same backend as Hindsight)"
	@$(COMPOSE) restart hermes

aux-status: ## Show current auxiliary model config
	@docker exec hermes hermes config show

validate: ## Validate docker-compose.yml (mirrors CI)
	@test -f .env || cp .env.example .env
	@$(COMPOSE) config -q && echo "✓ compose config valid"

lint: ## Ruff + py_compile the Windmill scripts (mirrors CI)
	@command -v ruff >/dev/null && ruff check windmill/ || echo "(install ruff for full lint)"
	@find windmill -name '*.py' -exec python3 -m py_compile {} + && echo "✓ python syntax OK"

test: ## Run windmill/tests/ (pip install -r windmill/tests/requirements.txt first; mirrors CI)
	@command -v pytest >/dev/null && (cd windmill && pytest tests/ -q) \
	  || echo "(install windmill/tests/requirements.txt for tests: pip install -r windmill/tests/requirements.txt)"

ci: validate lint test ## Run the same checks as GitHub Actions

collection-db-migrate: ## Apply pending version-controlled migrations to the running collection database
	@$(COMPOSE) ps --status running --services | grep -qx collection_db || { echo "✗ collection_db is not running"; exit 1; }
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  for migration in collection_db/migrations/*.up.sql; do \
	    [ -f "$$migration" ] || continue; \
	    echo "→ applying $$migration"; \
	    $(COMPOSE) exec -T collection_db psql -v ON_ERROR_STOP=1 \
	      -U "$${COLLECTION_DB_ADMIN_USER:-collection_admin}" \
	      -d "$${COLLECTION_DB_NAME:-collection}" < "$$migration"; \
	  done; \
	  echo "✓ collection database migrations applied"

windmill-push: ## Push windmill/ assets (resource type, resource, scripts) to the server — needs the wmill CLI
	@command -v wmill >/dev/null || { echo "✗ 'wmill' CLI not found — npm install -g windmill-cli"; exit 1; }
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  remote="$${WM_BASE_URL:-http://windmill.localhost}"; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  token=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	    -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	  if [ -n "$$token" ]; then (cd windmill && wmill workspace add main main "$$remote" --token "$$token" >/dev/null 2>&1) || true; \
	  else echo "→ default admin login failed — relying on your existing wmill profile (run 'wmill workspace add' once if none)"; fi; \
	  lock_backup=$$(mktemp -d); \
	  (cd windmill && find . -name '*.script.lock') | while read -r f; do \
	    mkdir -p "$$lock_backup/$$(dirname "$$f")"; cp "windmill/$$f" "$$lock_backup/$$f"; \
	  done; \
	  (cd windmill && wmill generate-metadata >/dev/null 2>&1) || true; \
	  (cd windmill && find . -name '*.script.lock') | while read -r f; do \
	    [ -f "$$lock_backup/$$f" ] || continue; \
	    old_n=$$(grep -vc '^#' "$$lock_backup/$$f" 2>/dev/null || echo 0); \
	    new_n=$$(grep -vc '^#' "windmill/$$f" 2>/dev/null || echo 0); \
	    if [ "$$old_n" -gt 0 ] && [ "$$new_n" -lt "$$old_n" ]; then \
	      echo "⚠ generate-metadata emptied windmill/$$f's pinned deps ($$old_n → $$new_n) — restoring (see docs/data-platform-add-pipeline.md)"; \
	      cp "$$lock_backup/$$f" "windmill/$$f"; \
	    fi; \
	  done; \
	  rm -rf "$$lock_backup"; \
	  esc=$$(printf '\033'); \
	  dels="$$( (cd windmill && wmill sync push --dry-run --yes --skip-branch-validation 2>&1) | sed "s/$${esc}\[[0-9;]*m//g" \
	    | grep -E '^- (folder|variable|resource|resource-type|script|flow|app|schedule|trigger|user|group|settings)( |$$)' || true)"; \
	  if [ -n "$$dels" ] && [ "$(FORCE)" != "1" ]; then \
	    echo "✗ push aborted — it would DELETE/ARCHIVE remote items not tracked in windmill/:"; \
	    printf '%s\n' "$$dels" | sed 's/^/    /'; \
	    echo "  Reconcile:  make windmill-pull         (bring them into the repo first)"; \
	    echo "  Or force:   make windmill-push FORCE=1 (destructive mirror)"; \
	    exit 1; \
	  fi; \
	  (cd windmill && wmill sync push --yes --skip-branch-validation) || { echo "✗ wmill sync push failed"; exit 1; }; \
	  echo "✓ pushed windmill/ assets"; \
	  if [ -n "$$token" ] && [ -n "$${API_SERVER_KEY:-}" ]; then \
	    vp="f/hermes/api_key"; \
	    if curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/create" \
	         -d "{\"path\":\"$$vp\",\"value\":\"$$API_SERVER_KEY\",\"is_secret\":true,\"description\":\"Hermes API key, used by Windmill scripts calling Hermes\"}" >/dev/null 2>&1; then \
	      echo "✓ created secret variable $$vp"; \
	    elif curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/update/$$vp" -d "{\"value\":\"$$API_SERVER_KEY\"}" >/dev/null 2>&1; then \
	      echo "✓ updated secret variable $$vp"; \
	    else echo "⚠ couldn't set $$vp — set it in the UI (Variables → $$vp)"; fi; \
	  fi; \
	  if [ -n "$$token" ] && [ -n "$${WINDMILL_COLLECTION_DB_PASSWORD:-}" ]; then \
	    vp="f/collection/db_password"; \
	    if curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/create" \
	         -d "{\"path\":\"$$vp\",\"value\":\"$$WINDMILL_COLLECTION_DB_PASSWORD\",\"is_secret\":true,\"description\":\"windmill_collection role password into collection_db's collection schema\"}" >/dev/null 2>&1; then \
	      echo "✓ created secret variable $$vp"; \
	    elif curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/update/$$vp" -d "{\"value\":\"$$WINDMILL_COLLECTION_DB_PASSWORD\"}" >/dev/null 2>&1; then \
	      echo "✓ updated secret variable $$vp"; \
	    else echo "⚠ couldn't set $$vp — set it in the UI (Variables → $$vp)"; fi; \
	    echo "✓ collection_db Postgres resource available to scripts/flows as f/collection/collection_db"; \
	    echo "  → Baserow webhook receiver deployed at f/collection/baserow_webhook. To wire a Baserow"; \
	    echo "    table to it: Baserow table → Webhooks → URL ="; \
	    echo "    http://windmill_server:8000/api/w/main/jobs/run/p/f/collection/baserow_webhook?token=$$token"; \
	    echo "    (use a dedicated Windmill token, not the admin login token above, for anything long-lived)"; \
	  fi; \
	  if [ -n "$$token" ] && [ -n "$${DATA_PLATFORM_DB_PASSWORD:-}" ]; then \
	    vp="f/data_platform/db_password"; \
	    if curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/create" \
	         -d "{\"path\":\"$$vp\",\"value\":\"$$DATA_PLATFORM_DB_PASSWORD\",\"is_secret\":true,\"description\":\"data_platform role password into collection_db's data_platform schema\"}" >/dev/null 2>&1; then \
	      echo "✓ created secret variable $$vp"; \
	    elif curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/update/$$vp" -d "{\"value\":\"$$DATA_PLATFORM_DB_PASSWORD\"}" >/dev/null 2>&1; then \
	      echo "✓ updated secret variable $$vp"; \
	    else echo "⚠ couldn't set $$vp — set it in the UI (Variables → $$vp)"; fi; \
	    echo "✓ data_platform Postgres resource available to scripts/flows as f/data_platform/data_platform_db"; \
	    echo "  → Example pipeline: run f/data_platform/extract_hn_stories then f/data_platform/dbt_run"; \
	  fi

windmill-pull: ## Pull windmill/ assets FROM the server into the repo for version control — needs the wmill CLI
	@command -v wmill >/dev/null || { echo "✗ 'wmill' CLI not found — npm install -g windmill-cli"; exit 1; }
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  remote="$${WM_BASE_URL:-http://windmill.localhost}"; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  token=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	    -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	  if [ -n "$$token" ]; then (cd windmill && wmill workspace add main main "$$remote" --token "$$token" >/dev/null 2>&1) || true; fi; \
	  (cd windmill && wmill sync pull --yes --skip-branch-validation) || { echo "✗ wmill sync pull failed"; exit 1; }; \
	  echo "✓ pulled windmill/ assets into windmill/ — review 'git diff' before committing"; \
	  echo "  skipSecrets keeps secret values out of YAML — only placeholders are written."

windmill-check: ## Report whether the live Windmill server has drifted from windmill/ in git (needs the wmill CLI + running server)
	@command -v wmill >/dev/null || { echo "✗ 'wmill' CLI not found — npm install -g windmill-cli"; exit 1; }
	@git diff --quiet -- windmill/ && git diff --cached --quiet -- windmill/ \
	  || { echo "✗ windmill/ has uncommitted changes — commit or stash them first so the check can revert cleanly"; exit 1; }
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  remote="$${WM_BASE_URL:-http://windmill.localhost}"; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  token=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	    -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	  if [ -n "$$token" ]; then (cd windmill && wmill workspace add main main "$$remote" --token "$$token" >/dev/null 2>&1) || true; fi; \
	  (cd windmill && wmill sync pull --yes --skip-branch-validation >/dev/null 2>&1) || { echo "✗ wmill sync pull failed (is the server running?)"; git checkout -- windmill/; exit 1; }; \
	  if git diff --quiet -- windmill/; then \
	    echo "✓ windmill/ is in sync with the server"; rc=0; \
	  else \
	    echo "✗ DRIFT: the server differs from windmill/ in git —"; git --no-pager diff --stat -- windmill/; \
	    echo "  Run 'make windmill-pull' to bring changes into the repo, or 'make windmill-push' to overwrite the server."; rc=1; \
	  fi; \
	  git checkout -- windmill/; exit $$rc

windmill-mcp: ## Register Windmill with Hermes as a native MCP server (idempotent; mints a narrowly-scoped token if none exists)
	@docker ps --format '{{.Names}}' | grep -qx windmill_server || { echo "✗ Windmill isn't running — run 'make up' first"; exit 1; }
	@docker exec hermes sh -c 'command -v hermes >/dev/null' 2>/dev/null || { echo "✗ 'hermes' CLI not found in the running Hermes container"; exit 1; }
	@set -a; . ./$(ENV_FILE); set +a; \
	  ENVF="$(ENV_FILE)"; \
	  envput() { if grep -qE "^$$1=" "$$ENVF"; then sed -i.bak "s|^$$1=.*|$$1=$$2|" "$$ENVF" && rm -f "$$ENVF.bak"; else { [ -s "$$ENVF" ] && [ -n "$$(tail -c1 "$$ENVF")" ] && printf '\n' >> "$$ENVF"; }; printf '%s=%s\n' "$$1" "$$2" >> "$$ENVF"; fi; }; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  MCP_URL="http://windmill_server:8000/api/mcp/w/main/sse"; \
	  if docker exec hermes hermes mcp list 2>/dev/null | grep -qE '^  windmill\b.*enabled' \
	     && docker exec hermes hermes mcp test windmill 2>&1 | grep -qi "Tools discovered"; then \
	    echo "→ Hermes already has a working 'windmill' MCP connection — leaving it as-is"; \
	    echo "  (to rotate it to the narrowly-scoped token this target mints, run"; \
	    echo "   'docker exec hermes hermes mcp remove windmill' then 'make windmill-mcp' again)"; \
	    exit 0; \
	  fi; \
	  TOKEN="$${WM_MCP_TOKEN}"; \
	  if [ -n "$$TOKEN" ] && curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$TOKEN" "$$base/api/w/main/scripts/list?per_page=1" >/dev/null 2>&1; then \
	    echo "→ reusing existing WM_MCP_TOKEN from $$ENVF"; \
	  else \
	    [ -n "$$TOKEN" ] && echo "→ WM_MCP_TOKEN in $$ENVF no longer works — minting a new one"; \
	    ADMIN=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	      -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	    [ -n "$$ADMIN" ] || { echo "✗ couldn't log in to Windmill as admin@windmill.dev — is the server up and still on the default admin credentials?"; \
	      echo "  If you've changed them, set WM_MCP_TOKEN in $$ENVF by hand (a token with mcp:all, scripts:read,"; \
	      echo "  flows:read, jobs:read, jobs:run:scripts, jobs:run:flows scopes, minted via the Windmill UI or API) and re-run."; exit 1; }; \
	    TOKEN=$$(curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$ADMIN" -H 'Content-Type: application/json' -X POST "$$base/api/users/tokens/create" \
	      -d '{"label":"windmill-mcp","scopes":["mcp:all","scripts:read","flows:read","jobs:read","jobs:run:scripts","jobs:run:flows"]}' 2>/dev/null); \
	    [ -n "$$TOKEN" ] || { echo "✗ token creation failed"; exit 1; }; \
	    envput WM_MCP_TOKEN "$$TOKEN"; \
	    echo "→ minted a Windmill token (mcp:all — required to reach the MCP endpoint at all — plus"; \
	    echo "  scripts:read, flows:read, jobs:read, jobs:run:scripts, jobs:run:flows; no *:write scope,"; \
	    echo "  so write-shaped MCP tools like createScript/createVariable are visible but 403 when called)"; \
	    echo "  — saved WM_MCP_TOKEN to $$ENVF"; \
	  fi; \
	  docker exec hermes sh -c "sed -i '/^MCP_WINDMILL_API_KEY=/d' /opt/data/.env 2>/dev/null" || true; \
	  docker exec hermes hermes mcp remove windmill >/dev/null 2>&1 || true; \
	  printf 'y\n%s\ny\n' "$$TOKEN" | docker exec -i hermes hermes mcp add windmill --url "$$MCP_URL" --auth header >/dev/null 2>&1; \
	  echo "→ verifying connection…"; \
	  docker exec hermes hermes mcp test windmill 2>&1 | grep -iE "Connected|Tools discovered" \
	    || { echo "⚠ couldn't confirm — check: docker exec hermes hermes mcp test windmill"; exit 0; }; \
	  echo "✓ Windmill MCP tools are available to Hermes (start a new session to use them)"

backup: ## Snapshot Postgres (Windmill + Hindsight + Collection) + the Hermes data dir into ./backups/
	@mkdir -p backups
	@set -a; . ./$(ENV_FILE); set +a; \
	  STAMP=$$(date +%F-%H%M); \
	  echo "→ dumping Windmill Postgres…"; \
	  $(COMPOSE) exec -T db pg_dump -U postgres windmill | gzip > "backups/windmill-$$STAMP.sql.gz"; \
	  echo "→ dumping Hindsight Postgres…"; \
	  $(COMPOSE) exec -T hindsight_db pg_dump -U "$${HINDSIGHT_DB_USER:-hindsight}" "$${HINDSIGHT_DB_NAME:-hindsight}" | gzip > "backups/hindsight-$$STAMP.sql.gz"; \
	  echo "→ dumping Collection Postgres (Baserow + Directus + shared collection schemas)…"; \
	  $(COMPOSE) exec -T collection_db pg_dump -U "$${COLLECTION_DB_ADMIN_USER:-collection_admin}" "$${COLLECTION_DB_NAME:-collection}" | gzip > "backups/collection-$$STAMP.sql.gz"; \
	  echo "→ archiving Hermes /data…"; \
	  tar czf "backups/hermes-$$STAMP.tar.gz" -C "$${DATA_DIR:-$$HOME/.hermes/data}" . ; \
	  echo "✓ backups written to ./backups/ (stamp $$STAMP)"; \
	  keep_days="$${BACKUP_RETENTION_DAYS:-14}"; \
	  pruned=$$(find backups -maxdepth 1 -type f \( -name 'windmill-*.sql.gz' -o -name 'hindsight-*.sql.gz' -o -name 'collection-*.sql.gz' -o -name 'hermes-*.tar.gz' \) -mtime "+$$keep_days" -print -delete); \
	  if [ -n "$$pruned" ]; then echo "→ pruned backups older than $$keep_days days:"; printf '%s\n' "$$pruned" | sed 's/^/    /'; fi

backup-schedule: ## Install a daily cron job (03:00) that runs `make backup` automatically — needs cron
	@command -v crontab >/dev/null || { echo "✗ 'crontab' not found on this system"; exit 1; }
	@dir="$$(pwd)"; mkdir -p backups; \
	  marker="# hermesflow-backup:$$dir"; \
	  mk="$$(command -v make)"; \
	  docker_dir="$$(dirname "$$(command -v docker 2>/dev/null)" 2>/dev/null)"; \
	  cron_path="/usr/local/bin:/usr/bin:/bin"; \
	  [ -n "$$docker_dir" ] && cron_path="$$docker_dir:$$cron_path"; \
	  cronline="0 3 * * * /bin/sh -c 'export PATH=\"$$cron_path\"; cd $$dir && $$mk backup' >> $$dir/backups/backup-cron.log 2>&1 $$marker"; \
	  ( crontab -l 2>/dev/null | grep -vF "$$marker" ; echo "$$cronline" ) | crontab -; \
	  echo "✓ scheduled: daily backup at 03:00 → $$dir/backups/backup-cron.log"; \
	  echo "  cron PATH includes: $$cron_path"; \
	  echo "  Revert with: make backup-schedule-revert"

backup-schedule-revert: ## Remove the cron job installed by backup-schedule
	@command -v crontab >/dev/null || { echo "✗ 'crontab' not found on this system"; exit 1; }
	@dir="$$(pwd)"; marker="# hermesflow-backup:$$dir"; \
	  if crontab -l 2>/dev/null | grep -qF "$$marker"; then \
	    crontab -l 2>/dev/null | grep -vF "$$marker" | crontab -; \
	    echo "✓ removed scheduled backup cron job"; \
	  else \
	    echo "→ no scheduled backup cron job found for $$dir"; \
	  fi

bootstrap: ## One-shot: check → init → secrets → wizard → secure → pull → build → up → heal → health
	@$(MAKE) init
	@$(MAKE) secrets
	@echo
	@read -p "Have you filled in provider keys in .env (or will use the wizard)? [y/N] " ok; \
	  [[ "$$ok" == "y" || "$$ok" == "Y" ]] || { echo "Edit .env first, then re-run 'make bootstrap'."; exit 1; }
	@$(MAKE) wizard
	@$(MAKE) secure
	@$(MAKE) pull
	@$(MAKE) build
	@$(MAKE) up
	@$(MAKE) hermes-heal
	@$(MAKE) hermes-workspace
	@$(MAKE) hermes-secure
	@sleep 8
	@$(MAKE) health
	@echo
	@read -p "Configure Hermes to route through Headroom now? [y/N] " hr; \
	  [[ "$$hr" == "y" || "$$hr" == "Y" ]] && $(MAKE) --no-print-directory headroom || true
