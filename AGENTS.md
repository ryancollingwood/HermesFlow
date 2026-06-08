# HermesFlow — Agent Guidelines

This file tells AI coding agents (Claude Code, Copilot, etc.) about the
conventions that must be followed when modifying this repository. Read it
before touching any file.

---

## Stack overview

| Layer | Services |
|---|---|
| Ingress | `caddy` |
| Agent | `hermes` |
| Memory | `hindsight` |
| Workflow | `windmill_server`, `windmill_lsp`, `windmill_worker` (×2), `windmill_worker_native` |
| Data | `db` (PostgreSQL 16) |
| Observability | `prometheus`, `grafana`, `cadvisor`, `node_exporter`, `postgres_exporter` |

Networks: `backend`, `edge`, `agent`, `memory`, `monitoring`.

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
| Scrape config | `prometheus/prometheus.yml` |
| Datasource provisioning | `grafana/provisioning/datasources/prometheus.yml` |
| Dashboard auto-load directory | `grafana/provisioning/dashboards/` |
| Grafana credentials | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in `.env` |
| Metrics retention | `PROMETHEUS_RETENTION` in `.env` (default 15d) |

cAdvisor provides container-level metrics for **all** services automatically —
no per-service action is needed for CPU, memory, network, and disk I/O.

---

## Validation before committing

```bash
# Confirm the compose file is syntactically valid
docker compose config --quiet

# Confirm every service has resource limits
docker compose config | grep -A5 'deploy:' | grep -c 'cpus'

# Confirm every non-monitoring service has logging configured
docker compose config | grep 'driver: json-file' | wc -l
```
