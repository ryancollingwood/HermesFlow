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

.PHONY: help check init apikey secrets wizard secure fix-permissions pull build up down restart logs ps health backup bootstrap hermes-heal lint validate ci headroom headroom-revert mlx mlx-revert mlx-status memory memory-revert hindsight-mlx hindsight-mlx-revert windmill-push windmill-pull windmill-check

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
	           "$${SHARED_DIR:-$$DOCKER_HOME/.shared_agent_data}" \
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

mlx: ## Route Hermes to a host-native MLX server (Apple Silicon — see mlx/README.md)
	@set -a; . ./$(ENV_FILE); set +a; \
	  docker exec hermes hermes config set model.provider custom; \
	  docker exec hermes hermes config set model.base_url "$${MLX_BASE_URL:-http://host.docker.internal:8080/v1}"; \
	  docker exec hermes hermes config set model.api_key mlx
	@$(COMPOSE) restart hermes
	@echo "✓ Hermes is routing to the host MLX server"
	@echo "  Make sure mlx_lm.server is running on the host first — see mlx/README.md"

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

validate: ## Validate docker-compose.yml (mirrors CI)
	@test -f .env || cp .env.example .env
	@$(COMPOSE) config -q && echo "✓ compose config valid"

lint: ## Ruff + py_compile the Windmill scripts (mirrors CI)
	@command -v ruff >/dev/null && ruff check windmill/ || echo "(install ruff for full lint)"
	@find windmill -name '*.py' -exec python3 -m py_compile {} + && echo "✓ python syntax OK"

ci: validate lint ## Run the same checks as GitHub Actions

windmill-push: ## Push windmill/ assets (resource type, resource, scripts) to the server — needs the wmill CLI
	@command -v wmill >/dev/null || { echo "✗ 'wmill' CLI not found — npm install -g windmill-cli"; exit 1; }
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  remote="$${WM_BASE_URL:-http://windmill.localhost}"; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  token=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	    -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	  if [ -n "$$token" ]; then (cd windmill && wmill workspace add main main "$$remote" --token "$$token" >/dev/null 2>&1) || true; \
	  else echo "→ default admin login failed — relying on your existing wmill profile (run 'wmill workspace add' once if none)"; fi; \
	  (cd windmill && wmill generate-metadata >/dev/null 2>&1) || true; \
	  esc=$$(printf '\033'); \
	  dels="$$( (cd windmill && wmill sync push --dry-run --yes 2>&1) | sed "s/$${esc}\[[0-9;]*m//g" \
	    | grep -E '^- (folder|variable|resource|resource-type|script|flow|app|schedule|trigger|user|group|settings)( |$$)' || true)"; \
	  if [ -n "$$dels" ] && [ "$(FORCE)" != "1" ]; then \
	    echo "✗ push aborted — it would DELETE/ARCHIVE remote items not tracked in windmill/:"; \
	    printf '%s\n' "$$dels" | sed 's/^/    /'; \
	    echo "  Reconcile:  make windmill-pull         (bring them into the repo first)"; \
	    echo "  Or force:   make windmill-push FORCE=1 (destructive mirror)"; \
	    exit 1; \
	  fi; \
	  (cd windmill && wmill sync push --yes) || { echo "✗ wmill sync push failed"; exit 1; }; \
	  echo "✓ pushed windmill/ assets"; \
	  if [ -n "$$token" ] && [ -n "$${API_SERVER_KEY:-}" ]; then \
	    vp="f/hermes/api_key"; \
	    if curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/create" \
	         -d "{\"path\":\"$$vp\",\"value\":\"$$API_SERVER_KEY\",\"is_secret\":true}" >/dev/null 2>&1; then \
	      echo "✓ created secret variable $$vp"; \
	    elif curl -fsS -H "Host: $$hh" -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
	         -X POST "$$base/api/w/main/variables/update/$$vp" -d "{\"value\":\"$$API_SERVER_KEY\"}" >/dev/null 2>&1; then \
	      echo "✓ updated secret variable $$vp"; \
	    else echo "⚠ couldn't set $$vp — set it in the UI (Variables → $$vp)"; fi; \
	  fi

windmill-pull: ## Pull windmill/ assets FROM the server into the repo for version control — needs the wmill CLI
	@command -v wmill >/dev/null || { echo "✗ 'wmill' CLI not found — npm install -g windmill-cli"; exit 1; }
	@set -a; . ./$(ENV_FILE) 2>/dev/null; set +a; \
	  remote="$${WM_BASE_URL:-http://windmill.localhost}"; \
	  base="http://127.0.0.1:$${CADDY_HTTP_PORT:-80}"; hh="windmill.localhost"; \
	  token=$$(curl -fsS -H "Host: $$hh" -H 'Content-Type: application/json' -X POST "$$base/api/auth/login" \
	    -d '{"email":"admin@windmill.dev","password":"changeme"}' 2>/dev/null | tr -d '"'); \
	  if [ -n "$$token" ]; then (cd windmill && wmill workspace add main main "$$remote" --token "$$token" >/dev/null 2>&1) || true; fi; \
	  (cd windmill && wmill sync pull --yes) || { echo "✗ wmill sync pull failed"; exit 1; }; \
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
	  (cd windmill && wmill sync pull --yes >/dev/null 2>&1) || { echo "✗ wmill sync pull failed (is the server running?)"; git checkout -- windmill/; exit 1; }; \
	  if git diff --quiet -- windmill/; then \
	    echo "✓ windmill/ is in sync with the server"; rc=0; \
	  else \
	    echo "✗ DRIFT: the server differs from windmill/ in git —"; git --no-pager diff --stat -- windmill/; \
	    echo "  Run 'make windmill-pull' to bring changes into the repo, or 'make windmill-push' to overwrite the server."; rc=1; \
	  fi; \
	  git checkout -- windmill/; exit $$rc

backup: ## Snapshot Postgres (Windmill + Hindsight) + the Hermes data dir into ./backups/
	@mkdir -p backups
	@set -a; . ./$(ENV_FILE); set +a; \
	  STAMP=$$(date +%F-%H%M); \
	  echo "→ dumping Windmill Postgres…"; \
	  $(COMPOSE) exec -T db pg_dump -U postgres windmill | gzip > "backups/windmill-$$STAMP.sql.gz"; \
	  echo "→ dumping Hindsight Postgres…"; \
	  $(COMPOSE) exec -T hindsight_db pg_dump -U "$${HINDSIGHT_DB_USER:-hindsight}" "$${HINDSIGHT_DB_NAME:-hindsight}" | gzip > "backups/hindsight-$$STAMP.sql.gz"; \
	  echo "→ archiving Hermes /data…"; \
	  tar czf "backups/hermes-$$STAMP.tar.gz" -C "$${DATA_DIR:-$$HOME/.hermes/data}" . ; \
	  echo "✓ wrote backups/windmill-$$STAMP.sql.gz, backups/hindsight-$$STAMP.sql.gz, and backups/hermes-$$STAMP.tar.gz"

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
	@sleep 8
	@$(MAKE) health
	@echo
	@read -p "Configure Hermes to route through Headroom now? [y/N] " hr; \
	  [[ "$$hr" == "y" || "$$hr" == "Y" ]] && $(MAKE) --no-print-directory headroom || true
