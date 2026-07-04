# Joshu — AI cloud-desktop workspace (optional service)

[Joshu](https://github.com/db-aeon/joshu-oss) is an open-source, self-hosted "AI
desktop": a web desktop (ArozOS) that **you and an AI agent operate together**,
with a sandboxed browser (Camofox), a semantic file index (GBrain), an email
client, whiteboard, and an executive-assistant skill set — all driven by a
Hermes agent.

HermesFlow runs it as a standard opt-in override (`docker-compose.joshu.yml`),
the same pattern as Baserow/Directus/Ollama.

```bash
git clone https://github.com/db-aeon/joshu-oss ~/Projects/joshu-oss
# set JOSHU_AROZ_USER=<your-email> in .env (the desktop owner)
make joshu-build     # build the image from the clone (20-30+ min first time)
make joshu           # enable the override + start it
```

The installers also accept `--with-joshu` (`./install.sh --with-joshu …`),
which layers the override only when the image already exists — on a fresh host
it prints the build steps above instead of failing the install.

| URL | What |
|---|---|
| http://joshu.localhost | ArozOS desktop (Joshu API under `/joshu/*`) |
| http://joshu-admin.localhost | Joshu's **bundled** Hermes admin dashboard |
| http://localhost:8790 | ArozOS direct (bypassing Caddy) |
| http://localhost:8788 | Joshu API direct |

**Requirements:** ~8 GB RAM for the container (`JOSHU_MEM_LIMIT`), ~20 GB disk
(image + browser + index), Node 20+, npm **and yarn** (`npm install -g yarn` —
the vendored excalidraw fork builds with yarn classic) on the host to build the
image, and an `OPENROUTER_API_KEY` in `.env` (default provider — see
[LLM wiring](#llm-wiring)).

**Licensing note:** joshu-oss is AGPL-3.0 and the image build includes the
repo's `proprietary/` directory (dual-licensed by upstream). Fine for
self-hosting; review the upstream `COMMERCIAL_LICENSE.md` before offering it as
a service.

---

## Why two Hermes gateways

After `make joshu` the stack contains **two** Hermes gateways, by design:

- **`hermes`** (http://hermes.localhost) — the stack's own gateway: your
  messaging integrations, Windmill wiring, Headroom routing, Hindsight memory.
- **Joshu's bundled gateway** (http://joshu-admin.localhost) — pinned +
  patched inside `joshu-stack`, driving only the Joshu desktop.

They cannot be merged by configuration. Joshu:

- spawns its gateway as an **in-container child process** hardcoded to
  `127.0.0.1` (`src/hermesApi.ts` `startGateway()`);
- continuously **writes into its gateway's local filesystem** — skills
  denylist (~160 bundled skills disabled), toolsets, MCP registrations
  (GBrain `:8794`, connectors `:8795`), Camofox identity — none of which can
  target another container;
- builds its gateway from a **pinned hermes-agent commit with five patches**
  (Langfuse tracing ×3, OpenRouter usage, content filter) plus a kanban
  WebSocket shim, while the stack's `hermes` image tracks
  `nousresearch/hermes-agent:latest` unpatched;
- runs kanban/cron bridges that exec the **local** hermes venv Python.

A chat-only remote mode exists upstream (`HERMES_API_AUTO_START=false` +
`HERMES_API_BASE_URL`), but it loses GBrain/connectors MCP, kanban, skills,
browser control and memory — everything that makes Joshu interesting. Treat it
as experimental; real gateway sharing needs upstream joshu-oss changes.

The gateways don't conflict: Joshu's stays on the bridge network (its `8642` /
`9119` are never published to the host), and its secret is
`JOSHU_HERMES_API_KEY` — **never reuse the stack's `API_SERVER_KEY`**.

## Building and updating the image

There is no public joshu-oss image — you build it from your clone
(`JOSHU_SRC_DIR` in `.env`, default `~/Projects/joshu-oss`):

```bash
make joshu-build    # npm ci → vps:predeploy → vps:build-image → JOSHU_IMAGE_REF
make joshu-sync     # git pull --ff-only → pin-delta report → rebuild → restart → health poll
```

`joshu-sync` refuses to run on a dirty clone, reports `deploy/RELEASE.json`
version/pin changes (hermes-agent, GBrain, Camofox base), skips the rebuild
when nothing changed (`JOSHU_FORCE_REBUILD=1` to override), and only restarts
`joshu-stack` if the override is enabled in `COMPOSE_FILE`.

**Apple Silicon:** upstream's build script forces `--platform linux/amd64`, so
the default path builds (and runs) under emulation — slow. Try
`JOSHU_BUILD_NATIVE=1 make joshu-build` for a native-arch build (plain
`docker build` with the same pins, skips the voice image). It works only if the
pinned Camofox base image publishes an arm64 manifest; if the build fails
resolving the base, fall back to the emulated default.

## First boot

First boot takes 5–10 minutes: the bundled gateway syncs its config and skills
denylist, GBrain indexes the desktop, Camofox warms up. Watch with
`docker logs -f joshu-stack`; the healthcheck gives it a 300 s grace period.

Smoke checks:

```bash
curl -fsS http://joshu.localhost/joshu/api/instance/health | jq '.healthy'
docker exec joshu-stack curl -fsS http://127.0.0.1:9377/health          # Camofox
docker exec joshu-stack sh -c \
  'curl -fsS -H "Authorization: Bearer $HERMES_API_KEY" http://127.0.0.1:8642/health'  # bundled gateway
```

Then open http://joshu.localhost, create the ArozOS account, and try a chat in
the Hermes Chat desktop app.

`make joshu-revert` stops the container and drops the override; everything
persists in `JOSHU_DATA_DIR` (desktop files, gateway config/skills, GBrain
index). Note the container runs as root, so those directories are root-owned
on the host — `sudo` for manual backups.

## LLM wiring

Default is **OpenRouter** (`JOSHU_HERMES_PROVIDER=openrouter`,
`JOSHU_HERMES_MODEL`), reusing the stack's `OPENROUTER_API_KEY`. Joshu's
executive-assistant features are tuned for capable hosted models; that default
is the supported path. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY` are also passed through for other providers.

### Advanced: point Joshu's agent at the stack's Ollama

Fully local, but expect degraded agent quality on small models — Joshu's skill
prompts are long. Needs the Ollama override running (`make ollama`) with a
capable model pulled (e.g. `qwen2.5:14b`).

1. Give `joshu-stack` a route to Ollama — in `docker-compose.joshu.yml`, change
   `networks: [edge]` to `networks: [edge, inference]`.
2. Joshu rewrites its gateway's managed config on every boot, but merges a
   **user overlay** on top (`migrateHermesUserConfig`). Write the overlay:

   ```bash
   docker exec joshu-stack sh -c 'cat > /root/.hermes/config.user.yaml <<EOF
   model:
     provider: custom
     base_url: http://ollama:11434/v1
     api_key: ollama
     model: qwen2.5:14b
   EOF'
   docker compose restart joshu-stack
   ```

3. Revert by deleting `/root/.hermes/config.user.yaml` (inside the container or
   under `$JOSHU_DATA_DIR/hermes/`) and restarting.

## Shared Hindsight memory (`make joshu-memory`)

By default Joshu's memory is off (`JOSHU_HINDSIGHT_ENABLED=false`) — its
internal Hindsight is a slim build that needs external Gemini/Cohere keys, and
running it would duplicate the stack's Hindsight + Postgres inside the fat
container.

Instead, Joshu uses **the stack's** Hindsight: its memory lands in a separate
`joshu` bank (`JOSHU_HINDSIGHT_BANK_ID`), isolated from the stack hermes's
memories, and it gets the stack's local embeddings/reranking for free. The
override is pre-wired — Joshu's boot sees the remote `HINDSIGHT_API_URL`
healthy and never starts its internal Hindsight, and the deliberately-inert
`HINDSIGHT_API_DATABASE_URL` guarantees the in-container Postgres stays off
and Joshu's fallback API can never attach to the stack's live database.

```bash
make joshu-memory          # needs joshu-stack AND the base stack's hindsight running
make joshu-memory-revert   # turn it back off (the joshu bank is preserved)
```

Before enabling, consider pinning `HINDSIGHT_VERSION` in `.env` near `0.7.x` —
Joshu's baked `hindsight-client` is 0.7.2 and a `:latest` server can drift
API-incompatible (pin changes take effect on the next
`docker compose up -d hindsight`; check compatibility with your existing
memory data before downgrading an established instance).

Verify:

```bash
docker exec joshu-stack curl -fsS http://hindsight:8888/health
docker exec joshu-stack pgrep -f "postgres" || echo "OK: no in-container postgres"
# hindsight.localhost UI should list a separate "joshu" bank once Joshu writes memories
```

## Troubleshooting

- **Unhealthy after 5 min** — first boot can exceed the grace period on slow
  disks/emulation: `docker logs joshu-stack --tail 100`. The health endpoint
  reports per-component status:
  `curl -s http://localhost:8788/joshu/api/instance/health | jq '.components'`.
- **Health stays 503 with `hermes`/`gbrain` failing and the log repeats
  `JOSHU_AROZ_USER is required`** — the desktop owner email isn't set. The boot
  gates the bundled gateway and GBrain on the owner's desktop paths. Set
  `JOSHU_AROZ_USER` in `.env` and recreate
  (`docker compose up -d --force-recreate joshu-stack`).
- **`joshu-admin.localhost` shows "Invalid Host header"** — the Caddy route's
  `header_up Host 127.0.0.1:9119` rewrite is missing; re-check the Caddyfile.
- **Chat replies fail** — the bundled gateway has no provider key: confirm
  `OPENROUTER_API_KEY` is set in `.env` and recreate
  (`docker compose up -d joshu-stack`).
- **`hermes` component false right after boot** — on slow/emulated hosts the
  boot script's 90 s gateway window can lapse before the gateway is up, and it
  isn't revived until something touches a Hermes endpoint. Trigger it:
  `curl -s http://localhost:8788/joshu/api/hermes-chat/status` (opening the
  desktop chat does the same).
- **`[gbrain] missing embedding API key` in the log** — GBrain's semantic file
  index needs `GEMINI_API_KEY` (or `OPENAI_API_KEY`) in `.env`. Boot and chat
  work without it, but semantic file search stays empty.
- **8787 confusion** — inside the container ArozOS is 8787, but on the host
  that port belongs to Headroom; Joshu's desktop is published on
  `JOSHU_AROZ_PORT` (8790).
