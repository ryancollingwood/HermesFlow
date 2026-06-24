# Hermes Python packages — how & why we bake them into a derived image

Hermes runs from a **thin derived image** ([`hermes/Dockerfile`](Dockerfile))
built on top of the upstream `nousresearch/hermes-agent` image. Extra Python
packages are declared in [`requirements.txt`](requirements.txt) and baked into
the agent's virtualenv at **build time**. This page explains why that indirection
exists and how to add a package safely.

## System & npm tools (tmux, claude-code, opencode, codex)

The same Dockerfile also bakes in `tmux` (apt) and the `@anthropic-ai/claude-code`
/ `opencode-ai` CLIs (npm) — system/npm tools, unrelated to the
`requirements.txt` / `LAZY_DEPS` mechanism below since they don't touch
`/opt/hermes/.venv`. See [CODING_AGENTS.md](CODING_AGENTS.md) for what's baked
in vs. not, why tmux is required, and how to authenticate each CLI
(claude-code, opencode, and codex).

## TL;DR

1. Find the package's pinned spec in Hermes' `LAZY_DEPS` allowlist
   (`tools/lazy_deps.py` inside the image).
2. Add that exact spec to [`requirements.txt`](requirements.txt).
3. Redeploy: `docker compose build hermes && docker compose up -d hermes`.
4. Verify (see [Verifying](#verifying)).

## Why runtime installs don't work

The upstream image is hardened with **two independent layers** that both block
the agent from installing packages at runtime:

1. **Lazy installs are disabled.** The image sets `HERMES_DISABLE_LAZY_INSTALLS=1`.
   Hermes' `tools/lazy_deps.py` checks this first and refuses to install —
   `config.yaml`'s `security.allow_lazy_installs: true` is never consulted in the
   container (it's a red herring).
2. **The venv is read-only.** `/opt/hermes/.venv` is owned by `root:root` and
   stripped of all write bits (`a-w`). The agent runs as the unprivileged
   `hermes` user (uid 501) and cannot write into it.

Both are deliberate. They stop an agent session — which can be steered by prompt
injection in any tool output (web pages, files, API responses) — from
self-modifying its own venv to install malicious packages that could intercept
API keys or exfiltrate data. **We keep both layers.**

## Why baking at build time is safe

Container image builds run as `root`, and root bypasses the venv's `a-w`
permission bits, so a build-time `uv pip install` can write into the venv even
though the runtime `hermes` user cannot. The Dockerfile then **re-applies the
hardening** (`chown -R root:root` + `chmod -R a-w`) to anything `uv` created.

The result: the runtime security posture is **identical** to upstream — read-only
root-owned venv, `HERMES_DISABLE_LAZY_INSTALLS=1` still set, agent gains zero new
write permissions — but the packages it needs are already present.

## Why pins must match `LAZY_DEPS`

Hermes gates optional backends through `lazy_deps.ensure("<feature>")`, which
checks whether the feature's packages are **already importable before** it ever
looks at the disable flag. If `requirements.txt` pins the package to the **same
version** listed in `LAZY_DEPS`, `ensure()` sees the feature as satisfied and
returns immediately — the disabled install path is never reached.

If you pin a **different** version, `ensure()` considers the feature unsatisfied
and tries to install it at runtime, which then fails against the read-only venv.
So: **always match the `LAZY_DEPS` pin exactly.**

To find the current pin for a feature:

```bash
docker exec hermes grep -n -B2 '<package-name>' /opt/hermes/tools/lazy_deps.py
```

## Adding a package

1. **Find the spec.** Look it up in `LAZY_DEPS` (command above). Example entry:
   `"search.firecrawl": ("firecrawl-py==4.17.0",)`.
2. **Add it** to [`requirements.txt`](requirements.txt), one spec per line, with
   a comment naming the feature it enables.
3. **Rebuild and restart:**
   ```bash
   docker compose build hermes && docker compose up -d hermes
   ```
4. **Verify** (below).

A feature with several packages (e.g. `platform.matrix`) needs **all** of its
specs added. Dependencies already present in the base venv are reused
automatically — only genuinely-new packages are downloaded.

## Verifying

```bash
# Package baked in & importable as the runtime (hermes) user:
docker exec -u hermes hermes /opt/hermes/.venv/bin/python -c \
  "import firecrawl, importlib.metadata as m; print(m.version('firecrawl-py'))"
# -> 4.17.0

# Feature now reported satisfied (so ensure() is a no-op):
docker exec -u hermes hermes /opt/hermes/.venv/bin/python -c \
  "from tools.lazy_deps import is_available; print(is_available('search.firecrawl'))"
# -> True

# Security posture unchanged:
docker exec hermes printenv HERMES_DISABLE_LAZY_INSTALLS          # -> 1
docker exec -u hermes hermes sh -c \
  'touch /opt/hermes/.venv/lib/python3.13/site-packages/.p'       # -> Permission denied
```

CI ([`.github/workflows/hermes-image.yml`](../.github/workflows/hermes-image.yml))
also builds the derived image on any change under `hermes/`, so a bad pin or a
package that fails to resolve is caught on push / PR.

## What NOT to do

- **Don't** unset `HERMES_DISABLE_LAZY_INSTALLS` or set
  `allow_lazy_installs` to re-enable runtime installs.
- **Don't** `chown`/`chmod` the venv to make it writable by `hermes`.
- **Don't** add a writable directory to `PYTHONPATH` to side-load packages — such
  a directory sorts ahead of the venv on `sys.path` and could shadow (and thus
  hijack) core packages.
- **Don't** add unpinned specs, URLs, `git+https://`, local paths, or
  `--index-url` overrides.

All of the above either weaken the security model or break the `LAZY_DEPS`
no-op behaviour. Baking pinned packages at build time is the supported path.

## Working directory: generated files belong in `/shared`

By default, Hermes gateway/cron sessions write files into whatever directory
the `gateway run` process was launched from — inside this image, that's
`/opt/hermes`, the read-only app/venv install dir. That pollutes the app's
own files instead of landing somewhere persistent and host-visible.

The compose stack already bind-mounts `/shared` (`${SHARED_DIR:-./data/shared}`
on the host) for exactly this purpose. `make hermes-workspace` sets Hermes'
`terminal.cwd` config key to `/shared` via `hermes config set` (the only
supported way to edit `config.yaml` — hand edits don't stick), so generated
files land under `./data/shared/` on the host instead of `/opt/hermes` or
`/opt/data`. `make bootstrap` runs this automatically; re-run
`make hermes-workspace` ad hoc on an existing deployment to apply it (it's
idempotent).

## Troubleshooting: stray package overlays (`hermes-heal`)

A Hermes **agent session** can't edit this Dockerfile or `docker-compose.yml`
from inside the container — its only writable lever is `/opt/data` (its home).
If it ever decides it needs a package, it may "self-heal" by installing into a
writable overlay (e.g. `/opt/data/.hermes-extras`) and adding
`PYTHONPATH=/opt/data/.hermes-extras` to `/opt/data/.env`.

**This is drift, not a fix, and it's actively harmful.** A `PYTHONPATH` directory
sorts at `sys.path[0]`, *ahead* of the venv, so its packages **shadow** the
venv's — including pinned, CVE-patched core dependencies. (Observed in practice:
an overlay's `aiohttp 3.14.1` shadowing the venv's CVE-pinned `aiohttp 3.13.4`.)
The `.env` edit lives in the bind-mounted data dir, so it **survives restarts**.

The baked venv is the single source of truth, so the overlay is also redundant.
Deployments neutralize this drift automatically:

```bash
make hermes-heal     # idempotent: removes /opt/data/.hermes-extras,
                     # strips the PYTHONPATH line from /opt/data/.env
                     # (backup .env.bak), and restarts hermes if it changed
```

`make bootstrap` runs this after `up`, and both installers run it too. Run it
ad-hoc any time you suspect overlay drift; on a clean stack it's a no-op. Verify
imports resolve from the venv (not the overlay):

```bash
docker exec -u hermes hermes /opt/hermes/.venv/bin/python -c \
  "import firecrawl, aiohttp; print(firecrawl.__file__); print('aiohttp', aiohttp.__version__)"
# firecrawl path under /opt/hermes/.venv/...  (NOT /opt/data); aiohttp 3.13.4
```

If the agent genuinely needs a new package, add it to `requirements.txt` and
rebuild (above) — never via an overlay.
