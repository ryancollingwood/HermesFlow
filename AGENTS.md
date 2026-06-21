# HermesFlow — Agent Guidelines

This file tells AI coding agents (Claude Code, Copilot, etc.) about the
conventions that must be followed when modifying this repository. Read it
before touching any file.

---

## Stack overview

| Layer | Services |
|---|---|
| Ingress | `caddy` |
| Agent | `hermes`, `headroom` |
| Memory | `hindsight`, `hindsight_db` |
| Inference | `ollama` |
| Workflow | `windmill_server`, `windmill_lsp`, `windmill_worker` (×2), `windmill_worker_native` |
| Data | `db` (PostgreSQL 16) |
| Observability | `prometheus`, `alertmanager`, `grafana`, `cadvisor`, `node_exporter`, `postgres_exporter`, `hindsight_postgres_exporter`, `loki`, `promtail` |

Networks: `backend`, `edge`, `agent`, `memory`, `inference`, `monitoring`.

---

## Installers & first-run

There are two non-interactive installers plus the interactive `make bootstrap`:

| Path | Notes |
|---|---|
| `install.sh` | bash; uses `make`, `openssl`, `curl`. |
| `install.py` | pure Python **standard library only** (Windows-friendly — no `make`/`bash`/`openssl`/`curl`). |
| `make bootstrap` | interactive Hermes setup wizard. |
| `INSTALL.md` | step-by-step walkthrough of the installers. |

Hard rules when touching the installers:

- **Keep `install.sh` and `install.py` at parity.** Same flags, same steps, same
  behaviour. A change to one must be mirrored in the other (and in the
  `--help`/docstring header), or don't make it.
- **`install.py` stays stdlib-only.** No `pip` dependencies — it must run on a
  fresh Windows host with just Python 3 + Docker Desktop.
- **Both are idempotent.** Re-running fills blanks only; never clobber existing
  secrets or user-set values.
- **Never run the real installer to "test" it** — it writes `.env`, creates
  dirs, pulls images, and starts/restarts containers. Validate with
  `--dry-run`, `bash -n install.sh`, and `python3 -m py_compile install.py`.
  `--dry-run` prints the resolved plan and exits before any side effect; it must
  stay genuinely side-effect-free.
- **Profiles** (`--profile minimal|full|gpu|mac|server|remote`) are presets that
  seed flag defaults *before* explicit flags, so explicit flags always win. Keep
  the preset table identical in both installers.
- Steps live in a fixed order (prereqs → model check → `.env` → secrets → data
  dirs → keys → up → probe → memory → windmill → mlx → headroom). New work slots
  into that sequence; reflect it in the header comment and `INSTALL.md`.

When you add or change installer behaviour, update **both** `README.md` (Quick
start / flags) and `INSTALL.md` (flag table + step description).

---

## Secrets & credential handling

- **Required secrets are auto-generated, never left to a weak compose default.**
  `make secrets` (and `install.py`'s `ensure_secret` calls) generate
  `API_SERVER_KEY`, `WM_DB_PASSWORD`, `HINDSIGHT_DB_PASSWORD`,
  `GRAFANA_ADMIN_PASSWORD` when blank or still a known-weak default. If you add a
  service that needs a password, add it to **both** places — do **not** ship a
  `${FOO:-changeme}` default in `docker-compose.yml` as the only protection.
- **Hermes reads provider keys and bot tokens from `<DATA_DIR>/.env`, not the
  top-level `.env`.** The `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` / `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` lines in the
  `hermes` service are intentionally **commented out** so they come from the
  wizard/installer-written `<DATA_DIR>/.env` (mapped to `/opt/data/.env`). Write
  Hermes-consumed secrets there, `chmod 600`, not to the top-level `.env`.
- **Never write a real secret into a tracked file.** `windmill/f/hermes/
  api_key.variable.yaml` keeps a placeholder; the real value is set server-side
  (UI/API), and `wmill.yaml` keeps `skipSecrets: true`. `.env`, `<DATA_DIR>`, and
  `backups/` are git-ignored — keep real keys only there.
- Generating a DB password only helps **before** the volume is initialized —
  Postgres applies `POSTGRES_PASSWORD` once, on first init. Rotating it later
  breaks auth. Installers therefore generate secrets before the first `up`.

---

## Optional features = compose overrides, not edits to the base file

Optional capabilities are layered with an override file toggled via `COMPOSE_FILE`
in `.env`, leaving the default behaviour untouched:

- `docker-compose.gpu.yml` adds the NVIDIA device reservation to `ollama`
  (`--gpu` sets `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml`). Docker
  Compose reads `COMPOSE_FILE` from `.env`, so every `docker compose` / `make`
  call picks it up. Prefer this pattern over uncommenting blocks in the base file.

Inference notes worth knowing: Docker Desktop on **macOS cannot pass the GPU**
into containers, so the bundled `ollama` is CPU-only there (use host-native
`mlx/` or remote inference). On a **Linux/NVIDIA** host, `--gpu` makes the
in-container `ollama` GPU-accelerated. The Hindsight memory provider reads
`HINDSIGHT_API_URL` (the `hindsight-client` package ships in the hermes image —
no `pip install` needed; just set `memory.provider`).

---

## Adding a new container

Work through **every** section below in order. Each section is a hard
requirement, not a suggestion.

### 1. Assign the right networks

Connect the service only to the networks it genuinely needs:

| The service needs to… | Add network |
|---|---|
| Be reachable through Caddy | `edge` |
| Read from / write to the Windmill database | `backend` |
| Call the Hermes OpenAI-compatible API | `agent` |
| Read or write Hindsight memory | `memory` |
| Be scraped by Prometheus | `monitoring` |

Never attach a service to a network for convenience — each extra attachment
is an attack surface.

### 2. Wire up observability (required)

Every new service must be observable. cAdvisor already emits per-container
CPU / memory / network / disk metrics automatically, so container-level
health is covered without any action. You must still do the following:

#### 2a. Application metrics

If the image exposes a Prometheus `/metrics` endpoint (check the image docs):

1. Add the service to the `monitoring` network.
2. Add a scrape job to `prometheus/prometheus.yml`:

```yaml
- job_name: <service-name>
  static_configs:
    - targets: ['<container-name>:<metrics-port>']
```

If the image does **not** expose metrics natively but is a well-known
technology, check whether a sidecar exporter exists on
[exporterhub.io](https://exporterhub.io) or the Prometheus community org
(`prometheuscommunity/<name>-exporter`). Use the pattern already established
by `postgres_exporter`: put the exporter in a separate service, connect it
to both `monitoring` and whichever application network the target is on, and
add `depends_on` pointing at the target service.

If no exporter exists, document why in a comment next to the service in
`docker-compose.yml` so the gap is visible.

#### 2b. Grafana dashboard

After wiring the scrape job, add (or reference) a dashboard:

- **Community dashboard**: note the Grafana dashboard ID in the comment
  block at the top of the observability section in `docker-compose.yml`
  (see the existing IDs for cAdvisor: 193, Node Exporter: 1860, PostgreSQL:
  9628). A human will import it via the Grafana UI.
- **Custom dashboard**: save the JSON to
  `grafana/provisioning/dashboards/<service-name>.json`. Grafana will
  auto-load it on the next restart because the provisioning provider watches
  that directory.

### 3. Set resource limits

Every service must declare a `deploy.resources.limits` block. Use these
bands as a starting point and adjust based on the image's documented
requirements:

| Role | CPU | Memory |
|---|---|---|
| Heavy workload (workers, ML inference) | 1.0–2.0 | 1–4 G |
| Application server / API | 0.5–1.0 | 256–1024 M |
| Lightweight exporter / sidecar | 0.1–0.3 | 64–256 M |

### 4. Add a healthcheck

Add a `healthcheck` if the image exposes any health or readiness endpoint.
Use `curl -fsS` or `wget -qO-` against the documented path. Example:

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://localhost:<port>/health"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 40s
```

If there is genuinely no health endpoint, omit the block and add a comment
explaining why.

### 5. Add a Caddy route (if user-facing)

If the service has a UI or an API that should be reachable through the
reverse proxy:

1. Add it to the `edge` network.
2. Add a route to `Caddyfile`:

```
http://<name>.localhost {
    reverse_proxy <container-name>:<port>
}
```

3. For services that carry sensitive data or have no built-in auth, add a
   comment noting that `basicauth` should be considered before exposing to
   a non-trusted network.

### 6. Document environment variables

Add every configurable variable to `.env.example` with:
- A short comment explaining what it does
- A safe default value
- A `${VAR_NAME_DATA_DIR}` path variable if the service writes state, so
  the operator can redirect it alongside the other data directories

If the variable is a **secret** (password / token / key):
- Mark it `# <REQUIRED>` and leave the value blank in `.env.example`.
- Add it to `make secrets` (an `ensure_secret` call) **and** the matching
  `ensure_secret(...)` in `install.py` so it's auto-generated — see
  [Secrets & credential handling](#secrets--credential-handling). Never rely on a
  weak `${FOO:-changeme}` default in `docker-compose.yml`.
- If Hermes consumes it (provider key, bot token), it must be read from
  `<DATA_DIR>/.env`, not the top-level `.env`.

### 7. Use the shared logging anchor

All services must include:

```yaml
logging: *default-logging
```

This gives JSON log output with automatic rotation (20 MB / 5 files,
gzip-compressed). Do not override the logging driver unless there is a
documented operational reason.

---

## Modifying an existing service

- **Changing networks**: re-read section 1 and section 2a — removing a
  network may silently break a Prometheus scrape job.
- **Changing ports**: update `prometheus/prometheus.yml` if the service
  exposes metrics, and update `Caddyfile` if there is a proxy route.
- **Removing a service**: also remove its scrape job from
  `prometheus/prometheus.yml`, its Caddy route, and its `.env.example`
  entries.

---

## Observability reference

| What | Where |
|---|---|
| Grafana UI | `http://grafana.localhost` |
| Prometheus UI | `http://prometheus.localhost` |
| Alertmanager UI | `http://alertmanager.localhost` |
| Scrape config | `prometheus/prometheus.yml` |
| Alert rules | `prometheus/alert.rules.yml` |
| Alert routing / receivers | `alertmanager/alertmanager.yml` (no real receiver configured by default) |
| Log aggregation | Loki + Promtail — query via Grafana **Explore** → `Loki` datasource, no separate UI |
| Loki / Promtail config | `loki/loki-config.yml`, `loki/promtail-config.yml` |
| Datasource provisioning | `grafana/provisioning/datasources/prometheus.yml` |
| Dashboard auto-load directory | `grafana/provisioning/dashboards/` |
| Grafana credentials | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in `.env` |
| Metrics retention | `PROMETHEUS_RETENTION` in `.env` (default 15d) |

cAdvisor provides container-level metrics for **all** services automatically —
no per-service action is needed for CPU, memory, network, and disk I/O.

---

## Validation before committing

```bash
# Compose (mirrors CI): syntax + ${VAR} expansion against .env.example
make validate                       # = docker compose config --quiet
make ci                             # validate + lint (ruff + py_compile of windmill/)

# Confirm every service has resource limits
docker compose config | grep -A5 'deploy:' | grep -c 'cpus'

# Confirm every non-monitoring service has logging configured
docker compose config | grep 'driver: json-file' | wc -l

# If a docker-compose override changed, validate the merged result too
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config -q
```

If you changed the installers:

```bash
bash -n install.sh                  # bash syntax
python3 -m py_compile install.py    # python syntax
./install.sh --dry-run              # preview; must make NO changes
```

Never "test" an installer by actually running it — use `--dry-run`. Keep
`install.sh`, `install.py`, `README.md`, and `INSTALL.md` in sync in the same
change.
