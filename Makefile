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

SKILL_DIR  := $(HOME)/.claude/skills
SKILL_REPO := https://github.com/mvanhorn/last30days-skill.git
SKILL_SRC  := $(SKILL_DIR)/last30days-skill

.PHONY: help check init apikey wizard secure pull up down restart logs ps health backup bootstrap lint validate ci install-skill update-skill

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
	  mkdir -p "$${DATA_DIR:-$$HOME/.hermes}" \
	           "$${SHARED_DIR:-$$HOME/.shared_agent_data}" \
	           "$${WM_DATA_DIR:-$$HOME/.windmill}"/{db,logs,cache} \
	           "$${WM_LSP_CACHE_DIR:-$$HOME/.windmill/lsp_cache}" \
	           "$${CADDY_DATA_DIR:-$$HOME/.caddy/data}" \
	           "$${CADDY_CONFIG_DIR:-$$HOME/.caddy/config}"; \
	  echo "✓ data directories ready"

apikey: ## Generate API_SERVER_KEY in .env if it's empty
	@grep -q '^API_SERVER_KEY=.\+' $(ENV_FILE) 2>/dev/null && { echo "→ API_SERVER_KEY already set"; exit 0; } || true
	@KEY=$$(openssl rand -hex 32); \
	  if grep -q '^API_SERVER_KEY=' $(ENV_FILE); then \
	    sed -i.bak "s|^API_SERVER_KEY=.*|API_SERVER_KEY=$$KEY|" $(ENV_FILE) && rm -f $(ENV_FILE).bak; \
	  else \
	    echo "API_SERVER_KEY=$$KEY" >> $(ENV_FILE); \
	  fi; \
	  echo "✓ generated API_SERVER_KEY"

wizard: ## Run the Hermes first-run setup wizard (interactive; writes ~/.hermes/.env + config)
	@set -a; . ./$(ENV_FILE); set +a; \
	  HERMES_DATA="$${DATA_DIR:-$$HOME/.hermes}"; \
	  docker run -it --rm \
	    -v "$$HERMES_DATA":/opt/data \
	    $(HERMES_IMAGE) setup

secure: ## chmod 600 the secret files
	@chmod 600 $(ENV_FILE) 2>/dev/null && echo "✓ chmod 600 .env" || true
	@set -a; . ./$(ENV_FILE); set +a; \
	  HERMES_DATA="$${DATA_DIR:-$$HOME/.hermes}"; \
	  test -f "$$HERMES_DATA/.env" && chmod 600 "$$HERMES_DATA/.env" && echo "✓ chmod 600 $$HERMES_DATA/.env" || true

pull: ## Pull the latest images
	@$(COMPOSE) pull

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

validate: ## Validate docker-compose.yml (mirrors CI)
	@test -f .env || cp .env.example .env
	@$(COMPOSE) config -q && echo "✓ compose config valid"

lint: ## Ruff + py_compile the Windmill scripts (mirrors CI)
	@command -v ruff >/dev/null && ruff check windmill/ || echo "(install ruff for full lint)"
	@find windmill -name '*.py' -exec python3 -m py_compile {} + && echo "✓ python syntax OK"

ci: validate lint ## Run the same checks as GitHub Actions

backup: ## Snapshot Postgres + the Hermes data dir into ./backups/
	@mkdir -p backups
	@set -a; . ./$(ENV_FILE); set +a; \
	  STAMP=$$(date +%F-%H%M); \
	  echo "→ dumping Postgres…"; \
	  $(COMPOSE) exec -T db pg_dump -U postgres windmill | gzip > "backups/windmill-$$STAMP.sql.gz"; \
	  echo "→ archiving Hermes /data…"; \
	  tar czf "backups/hermes-$$STAMP.tar.gz" -C "$${DATA_DIR:-$$HOME/.hermes/data}" . ; \
	  echo "✓ wrote backups/windmill-$$STAMP.sql.gz and backups/hermes-$$STAMP.tar.gz"

install-skill: ## Clone/update last30days skill and link it into ~/.claude/skills
	@mkdir -p $(SKILL_DIR)
	@if [ -d "$(SKILL_SRC)/.git" ]; then \
	  echo "→ updating last30days-skill…"; \
	  git -C $(SKILL_SRC) pull --ff-only; \
	else \
	  echo "→ cloning last30days-skill…"; \
	  git clone $(SKILL_REPO) $(SKILL_SRC); \
	fi
	@ln -sfn "$(SKILL_SRC)/skills/last30days" "$(SKILL_DIR)/last30days"
	@echo "✓ last30days skill ready — invoke with /last30days <topic>"

update-skill: ## Pull latest last30days skill and refresh the symlink
	@test -d "$(SKILL_SRC)/.git" || { echo "✗ skill not installed — run 'make install-skill' first"; exit 1; }
	@git -C $(SKILL_SRC) pull --ff-only
	@ln -sfn "$(SKILL_SRC)/skills/last30days" "$(SKILL_DIR)/last30days"
	@echo "✓ last30days skill updated"

bootstrap: ## One-shot: check → init → apikey → wizard → secure → pull → up → health → skill
	@$(MAKE) init
	@$(MAKE) apikey
	@echo
	@read -p "Have you filled in provider keys in .env (or will use the wizard)? [y/N] " ok; \
	  [[ "$$ok" == "y" || "$$ok" == "Y" ]] || { echo "Edit .env first, then re-run 'make bootstrap'."; exit 1; }
	@$(MAKE) wizard
	@$(MAKE) secure
	@$(MAKE) pull
	@$(MAKE) up
	@sleep 8
	@$(MAKE) health
	@$(MAKE) install-skill
