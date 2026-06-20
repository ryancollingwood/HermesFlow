#!/usr/bin/env python3
"""HermesFlow — non-interactive installer (cross-platform).

A Python port of install.sh for hosts where bash/make/openssl/curl aren't
available (notably native Windows). Uses only the standard library and talks to
Docker via `docker` / `docker compose`. Safe to re-run (idempotent): it only
fills blanks, never clobbers existing secrets.

Quick start:
    set OPENROUTER_API_KEY=sk-or-...   &&  python install.py        (Windows cmd)
    $env:OPENROUTER_API_KEY="sk-or-..."; python install.py          (PowerShell)
    OPENROUTER_API_KEY=sk-or-... python3 install.py                 (macOS/Linux)
    python install.py --provider openrouter --api-key sk-or-... --model openai/gpt-4o-mini

What it does (mirrors install.sh / `make bootstrap`, minus the TTY wizard):
    1.  check prerequisites (docker + docker compose v2)
    2.  validate the model against the provider's /models list (--skip-model-check)
    3.  create .env from .env.example
    4.  set HERMES_UID/GID to the host user (skipped on Windows)
    5.  generate every required secret (API_SERVER_KEY, WM_DB_PASSWORD, HINDSIGHT_DB_PASSWORD)
    6.  create data dirs (+ fix ownership on POSIX)
    7.  write the provider key into <DATA_DIR>/.env — the file Hermes reads
    8.  pull images + start the stack
    9.  set the default model and probe Hermes end-to-end
    10. pull Hindsight's Ollama models + enable it as the memory provider (--no-memory)
    11. prep Windmill: pre-install the worker Python, create the 'main' workspace,
        and register Windmill with Hermes over MCP (--no-windmill)

Optional Telegram channel (both required together):
    --telegram-bot-token <token>          BotFather token
    --telegram-allowed-users <id,id,...>  numeric user IDs allowed to talk to it

Optional MLX host inference server (Apple Silicon macOS only):
    --with-mlx                            install mlx-lm + always-on launchd agent

Optional Hindsight (memory) model overrides — written to .env before 'up':
    --hindsight-model <id>                set every Hindsight LLM scope to <id>
    --hindsight-retain-model <id>         override just the retain scope
    --hindsight-consolidation-model <id>  override just the consolidation scope
    --hindsight-reflect-model <id>        override just the reflect scope
    --hindsight-base-url <url>            Hindsight LLM endpoint (ollama/LMStudio/MLX)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── console markers (fall back to ASCII if the terminal can't do UTF-8) ───────
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
_UNI = "utf" in (sys.stdout.encoding or "").lower()
ARROW, OK, WARN, CROSS = ("→", "✓", "⚠", "✗") if _UNI else ("->", "[ok]", "[!]", "[x]")

IS_WINDOWS = platform.system() == "Windows"
ENV = Path(".env")

PROVIDERS = {
    # provider: (key var, default model, /models url, auth style)
    "openrouter": ("OPENROUTER_API_KEY", "openai/gpt-4o-mini",
                   "https://openrouter.ai/api/v1/models", "bearer"),
    "anthropic":  ("ANTHROPIC_API_KEY", "claude-sonnet-4-6",
                   "https://api.anthropic.com/v1/models", "anthropic"),
    "openai":     ("OPENAI_API_KEY", "gpt-4o-mini",
                   "https://api.openai.com/v1/models", "bearer"),
}


# ── small helpers ─────────────────────────────────────────────────────────────
def say(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(msg, file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd: list[str], *, capture: bool = False, check: bool = False,
        stdin_text: str | None = None, quiet: bool = True):
    """Run a command. Returns CompletedProcess. Never raises unless check=True."""
    return subprocess.run(
        cmd,
        input=stdin_text,
        text=True,
        capture_output=capture or quiet,
        check=check,
    )


def run_ok(cmd: list[str], stdin_text: str | None = None) -> bool:
    try:
        return run(cmd, stdin_text=stdin_text).returncode == 0
    except FileNotFoundError:
        return False


def out(cmd: list[str]) -> str:
    """Return stdout (stripped) or '' on failure."""
    try:
        p = run(cmd, capture=True)
        return p.stdout.strip() if p.returncode == 0 else ""
    except FileNotFoundError:
        return ""


# ── .env handling ─────────────────────────────────────────────────────────────
def env_lines() -> list[str]:
    return ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []


def env_write(lines: list[str]) -> None:
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _strip_inline_comment(value: str) -> str:
    # Shell sourcing treats ` #...` (whitespace then #) as a comment.
    return re.sub(r"\s+#.*$", "", value).strip()


def env_value(key: str, default: str = "") -> str:
    for ln in env_lines():
        if ln.startswith(key + "="):
            return _strip_inline_comment(ln[len(key) + 1:])
    return default


def env_set(key: str, value: str) -> None:
    lines = env_lines()
    for i, ln in enumerate(lines):
        if ln.startswith(key + "="):
            lines[i] = f"{key}={value}"
            env_write(lines)
            return
    lines.append(f"{key}={value}")
    env_write(lines)


def expand(value: str) -> str:
    """Expand ${VAR} the way docker compose does (with a ~ fallback for HOME)."""
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        val = os.environ.get(name)
        if val is None and name == "HOME":
            val = os.path.expanduser("~")
        return val if val is not None else ""
    return re.sub(r"\$\{([^}]+)\}", repl, value)


# ── steps ─────────────────────────────────────────────────────────────────────
def fetch_model_ids(models_url: str, api_key: str, auth_style: str) -> list[str]:
    headers = {}
    if auth_style == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(models_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    return re.findall(r'"id"\s*:\s*"([^"]+)"', body)


def validate_model(model: str, provider: str, models_url: str, api_key: str,
                   auth_style: str) -> None:
    say(f"{ARROW} validating model '{model}' against {provider}…")
    ids = fetch_model_ids(models_url, api_key, auth_style)
    if not ids:
        say(f"{WARN} could not fetch {provider} model list (network/auth?) — skipping check.")
        say("  The end-to-end probe later will still catch a bad model id.")
        return
    if model in ids:
        # Catalog presence != callable on your key/tier — the probe is the real test.
        say(f"{OK} model '{model}' is listed by {provider}")
        return
    base = model.split("/")[-1]
    coarse = base.rsplit("-", 1)[0]
    hits = [i for i in ids if base.lower() in i.lower()][:8]
    if not hits:
        hits = [i for i in ids if coarse.lower() in i.lower()][:8]
    if not hits:
        hits = ids[:8]
    print(f"{CROSS} model '{model}' is not offered by {provider}.", file=sys.stderr)
    print("  Did you mean one of:", file=sys.stderr)
    for h in hits:
        print(f"    {h}", file=sys.stderr)
    print(f"  Re-run with --model <id>, or --skip-model-check to bypass.", file=sys.stderr)
    sys.exit(1)


def ensure_secret(key: str, nbytes: int, weak: str = "") -> None:
    cur = env_value(key)
    if cur == "" or cur == weak:
        env_set(key, secrets.token_hex(nbytes))
        say(f"{OK} generated {key}")
    else:
        say(f"{ARROW} {key} already set")


def make_dirs_and_fix_perms() -> None:
    home = os.environ.get("HOME") or os.path.expanduser("~")

    def d(key: str, default: str) -> str:
        return expand(env_value(key, default)) or default

    data = d("DATA_DIR", f"{home}/.hermes")
    shared = d("SHARED_DIR", f"{home}/.shared_agent_data")
    wm = d("WM_DATA_DIR", f"{home}/.windmill")
    wm_lsp = d("WM_LSP_CACHE_DIR", f"{home}/.windmill/lsp_cache")
    caddy_d = d("CADDY_DATA_DIR", f"{home}/.caddy/data")
    caddy_c = d("CADDY_CONFIG_DIR", f"{home}/.caddy/config")

    targets = [data, shared, f"{wm}/db", f"{wm}/logs", f"{wm}/cache",
               wm_lsp, caddy_d, caddy_c]
    for t in targets:
        Path(t).mkdir(parents=True, exist_ok=True)
    say(f"{OK} data directories created")

    if IS_WINDOWS:
        say(f"{ARROW} Windows host — skipping chown (Docker Desktop maps the UID)")
        return
    uid = env_value("HERMES_UID", "1000")
    gid = env_value("HERMES_GID", "1000")
    mounts = []
    for i, t in enumerate([data, shared, wm, wm_lsp, caddy_d, caddy_c]):
        mounts += ["-v", f"{t}:/mnt/{i}"]
    chown_paths = " ".join(f"/mnt/{i}" for i in range(6))
    say(f"{ARROW} fixing ownership to {uid}:{gid} on bind-mount directories…")
    ok = run_ok(["docker", "run", "--rm", *mounts, "alpine:3", "sh", "-c",
                 f"chown -R {uid}:{gid} {chown_paths} && chmod -R u+rwX {chown_paths}"])
    say(f"{OK} ownership corrected" if ok else f"{WARN} could not fix ownership (non-fatal)")


def resolve_data_dir() -> str:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return expand(env_value("DATA_DIR", f"{home}/.hermes")) or f"{home}/.hermes"


def set_data_env(data_dir: str, key: str, value: str) -> Path:
    """Set KEY=VALUE in <DATA_DIR>/.env (the file Hermes reads). Returns the path."""
    df = Path(data_dir) / ".env"
    df.parent.mkdir(parents=True, exist_ok=True)
    lines = df.read_text(encoding="utf-8").splitlines() if df.exists() else []
    lines = [ln for ln in lines if not ln.startswith(key + "=")]
    lines.append(f"{key}={value}")
    df.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(df, 0o600)
    except Exception:
        pass
    return df


def write_provider_key(data_dir: str, key_var: str, api_key: str) -> None:
    df = set_data_env(data_dir, key_var, api_key)
    say(f"{OK} wrote {key_var} to {df}")


def wait_hermes_healthy() -> None:
    say(f"{ARROW} waiting for Hermes to become healthy…")
    for _ in range(40):
        s = out(["docker", "inspect", "-f", "{{.State.Health.Status}}", "hermes"])
        if s == "healthy":
            say(f"{OK} Hermes healthy")
            return
        time.sleep(5)
    die(f"{CROSS} Hermes did not become healthy — check 'docker logs hermes'")


def pull_hindsight_models() -> None:
    base_url = env_value("HINDSIGHT_LLM_BASE_URL")
    if "ollama" not in base_url:
        say(f"{ARROW} Hindsight LLM backend is '{base_url or 'unset'}', not Ollama — skipping model pull")
        return
    models = []
    for k in ("HINDSIGHT_LLM_MODEL", "HINDSIGHT_RETAIN_LLM_MODEL",
              "HINDSIGHT_CONSOLIDATION_LLM_MODEL", "HINDSIGHT_REFLECT_LLM_MODEL"):
        v = env_value(k)
        if v and v not in models:
            models.append(v)
    if not models:
        say(f"{ARROW} no Hindsight Ollama models configured — skipping")
        return
    present = {ln.split()[0] for ln in out(["docker", "exec", "ollama", "ollama", "list"]).splitlines()[1:] if ln.split()}
    for m in models:
        if m in present:
            say(f"{OK} Ollama model already present: {m}")
        else:
            say(f"{ARROW} pulling Ollama model '{m}' (can be large/slow)…")
            if not run_ok(["docker", "exec", "ollama", "ollama", "pull", m]):
                say(f"{WARN} failed to pull '{m}' — Hindsight extraction won't work until it's available")


# ── Windmill (HTTP helpers + setup) ───────────────────────────────────────────
def wm_http(method: str, path: str, *, bearer: str | None = None,
            json_body: dict | None = None, timeout: int = 20):
    """Call the Windmill API via Caddy on 127.0.0.1 with the right Host header."""
    base = f"http://127.0.0.1:{env_value('CADDY_HTTP_PORT', '80') or '80'}"
    headers = {"Host": "windmill.localhost"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception:
        return None, None


def setup_windmill() -> None:
    say(f"{ARROW} preparing Windmill workers (pre-installing Python to avoid first-run races)…")
    if run_ok(["docker", "compose", "exec", "-T", "--index", "1", "windmill_worker",
               "sh", "-c",
               "UV_PYTHON_INSTALL_DIR=/tmp/windmill/cache/py_runtime uv python install 3.12"]):
        say(f"{OK} worker Python 3.12 pre-installed into the shared cache")
    else:
        say(f"{WARN} could not pre-install worker Python (non-fatal — the first job will try).")

    # Wait for the Windmill API.
    for _ in range(24):
        st, _b = wm_http("GET", "/api/version")
        if st == 200:
            break
        time.sleep(5)

    st, body = wm_http("POST", "/api/auth/login",
                       json_body={"email": "admin@windmill.dev", "password": "changeme"})
    token = (body or "").strip().strip('"') if st == 200 else ""
    if not token:
        say(f"{ARROW} Windmill: default admin login didn't work (already customized?) — skipping workspace setup.")
        return

    _st, wsbody = wm_http("GET", "/api/workspaces/list", bearer=token)
    if wsbody and '"id":"main"' in wsbody:
        say(f"{OK} Windmill 'main' workspace already exists")
    else:
        cst, _ = wm_http("POST", "/api/workspaces/create", bearer=token,
                         json_body={"id": "main", "name": "main"})
        if cst in (200, 201):
            say(f"{OK} created Windmill 'main' workspace")
        else:
            say(f"{WARN} couldn't create the Windmill 'main' workspace — create it in the UI before 'wmill sync push'.")

    # Register Windmill with Hermes over MCP (idempotent).
    if "windmill" in out(["docker", "exec", "hermes", "hermes", "mcp", "list"]):
        say(f"{OK} Hermes already has the 'windmill' MCP server configured")
        return
    tst, tok = wm_http("POST", "/api/users/tokens/create", bearer=token,
                       json_body={"label": "hermes-mcp", "scopes": ["mcp:all"]})
    mcptoken = (tok or "").strip().strip('"') if tst in (200, 201) else ""
    if not mcptoken:
        say(f"{WARN} couldn't mint a Windmill MCP token — skipping Hermes↔Windmill MCP wiring.")
        return
    # `hermes mcp add` is interactive: 'y' (server needs auth), then the token.
    ok = run_ok(
        ["docker", "exec", "-i", "hermes", "hermes", "mcp", "add", "windmill",
         "--url", "http://windmill_server:8000/api/mcp/w/main/sse", "--auth", "header"],
        stdin_text=f"y\n{mcptoken}\n",
    )
    if ok:
        say(f"{OK} registered Windmill as an MCP server in Hermes (scripts/flows + admin API as tools)")
    else:
        say(f"{WARN} couldn't register the Windmill MCP server in Hermes — wire it up manually (see README).")


def setup_mlx() -> None:
    """Install the host-native MLX inference server (Apple Silicon macOS only).

    MLX must run on the host, not in a container — Docker Desktop on macOS does
    not pass the GPU through. Creates a venv, installs mlx-lm, and registers the
    always-on launchd agent.
    """
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        say(f"{WARN} --with-mlx is for Apple Silicon macOS only "
            f"(host is {platform.system()}/{platform.machine()}) — skipping.")
        return
    venv = os.environ.get("MLX_VENV_DIR") or str(Path.home() / ".mlx-venv")
    server = Path(venv) / "bin" / "mlx_lm.server"
    pip = str(Path(venv) / "bin" / "pip")
    say(f"{ARROW} setting up host-native MLX server (Apple Silicon)…")
    if not server.exists():
        if subprocess.run([sys.executable, "-m", "venv", venv]).returncode != 0:
            say(f"{WARN} could not create venv at {venv} — skipping MLX.")
            return
        subprocess.run([pip, "install", "-U", "pip"])
        if subprocess.run([pip, "install", "-U", "mlx-lm"]).returncode != 0:
            say(f"{WARN} failed to install mlx-lm — skipping MLX.")
            return
    say(f"{OK} mlx-lm installed in {venv}")
    env = dict(os.environ, MLX_VENV_BIN=str(Path(venv) / "bin"))
    rc = subprocess.run(["bash", "mlx/install-launchd.sh"], env=env).returncode
    if rc == 0:
        say(f"{OK} MLX server installed as an always-on launchd agent (model loads on first request)")
    else:
        say(f"{WARN} launchd install failed — start it manually with: ./mlx/serve.sh")
    say("  To route Hermes through MLX:    make mlx")
    say("  Or point Hindsight at MLX:      set HINDSIGHT_LLM_BASE_URL=${MLX_BASE_URL} in .env, then: docker restart hindsight")


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    os.chdir(Path(__file__).resolve().parent)

    ap = argparse.ArgumentParser(
        description="HermesFlow non-interactive installer (cross-platform).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--provider", choices=list(PROVIDERS), default="openrouter")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--no-pull", action="store_true", help="skip 'docker compose pull'")
    ap.add_argument("--skip-model-check", action="store_true")
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--no-windmill", action="store_true")
    ap.add_argument("--telegram-bot-token", default="",
                    help="BotFather token to enable the Hermes Telegram channel")
    ap.add_argument("--telegram-allowed-users", default="",
                    help="comma-separated numeric user IDs allowed to use the bot "
                         "(required together with --telegram-bot-token)")
    ap.add_argument("--with-mlx", action="store_true",
                    help="install the host-native MLX inference server (Apple Silicon macOS only)")
    ap.add_argument("--hindsight-model", default="",
                    help="set every Hindsight LLM scope to this model id")
    ap.add_argument("--hindsight-retain-model", default="", help="override the retain scope")
    ap.add_argument("--hindsight-consolidation-model", default="", help="override the consolidation scope")
    ap.add_argument("--hindsight-reflect-model", default="", help="override the reflect scope")
    ap.add_argument("--hindsight-base-url", default="",
                    help="Hindsight LLM endpoint (ollama / LM Studio / MLX)")
    args = ap.parse_args()

    key_var, default_model, models_url, auth_style = PROVIDERS[args.provider]
    model = args.model or default_model
    api_key = args.api_key or os.environ.get(key_var, "")

    # Telegram (optional): both required together — an allow-list is mandatory for
    # the channel (otherwise anyone who finds the bot could talk to your agent).
    tg_token = args.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_users = args.telegram_allowed_users or os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    if (tg_token or tg_users) and not (tg_token and tg_users):
        die(f"{CROSS} Telegram needs BOTH --telegram-bot-token and --telegram-allowed-users\n"
            "  (allowed user IDs are required for the Hermes Telegram channel).")

    # 1. prerequisites
    say(f"{ARROW} checking prerequisites…")
    from shutil import which
    if not which("docker"):
        die(f"{CROSS} 'docker' not found on PATH")
    if not run_ok(["docker", "compose", "version"]):
        die(f"{CROSS} 'docker compose' v2 not found")
    say(f"{OK} prerequisites OK")

    # 2. validate the model
    if args.skip_model_check:
        say(f"{ARROW} skipping model check (--skip-model-check)")
    elif not api_key:
        say(f"{ARROW} no API key — skipping model check")
    else:
        validate_model(model, args.provider, models_url, api_key, auth_style)

    # 3. .env
    if not ENV.exists():
        ENV.write_text(Path(".env.example").read_text(encoding="utf-8"), encoding="utf-8")
        say(f"{OK} created .env from .env.example")
    else:
        say(f"{ARROW} .env already exists — leaving it in place")

    # 4. host UID/GID (POSIX only)
    if IS_WINDOWS:
        say(f"{ARROW} Windows host — leaving HERMES_UID/GID at their .env values")
    else:
        env_set("HERMES_UID", str(os.getuid()))   # type: ignore[attr-defined]
        env_set("HERMES_GID", str(os.getgid()))   # type: ignore[attr-defined]
        say(f"{OK} set HERMES_UID={os.getuid()} HERMES_GID={os.getgid()}")  # type: ignore[attr-defined]

    # 4b. Hindsight model / backend overrides — written before 'up' so the
    # hindsight container starts with them and step 10 pulls the right models.
    hs_retain = args.hindsight_retain_model or args.hindsight_model
    hs_consol = args.hindsight_consolidation_model or args.hindsight_model
    hs_reflect = args.hindsight_reflect_model or args.hindsight_model
    hs_overrides = {
        "HINDSIGHT_LLM_MODEL": args.hindsight_model,
        "HINDSIGHT_RETAIN_LLM_MODEL": hs_retain,
        "HINDSIGHT_CONSOLIDATION_LLM_MODEL": hs_consol,
        "HINDSIGHT_REFLECT_LLM_MODEL": hs_reflect,
        "HINDSIGHT_LLM_BASE_URL": args.hindsight_base_url,
    }
    if any(hs_overrides.values()):
        for k, v in hs_overrides.items():
            if v:
                env_set(k, v)
        say(f"{OK} applied Hindsight model/backend overrides to .env")

    # 5. secrets
    ensure_secret("API_SERVER_KEY", 32)
    ensure_secret("WM_DB_PASSWORD", 32, weak="windmill")
    ensure_secret("HINDSIGHT_DB_PASSWORD", 16, weak="hindsight")

    # 6. data dirs + ownership
    make_dirs_and_fix_perms()
    data_dir = resolve_data_dir()

    # 7. provider key → <DATA_DIR>/.env
    if api_key:
        write_provider_key(data_dir, key_var, api_key)
    else:
        say(f"{WARN} no API key supplied for {args.provider} — set {key_var} or pass --api-key.")
        say(f"  Add it to {Path(data_dir) / '.env'} later and run: docker restart hermes")

    # Telegram channel (optional) — written to the same /opt/data/.env Hermes reads.
    if tg_token:
        set_data_env(data_dir, "TELEGRAM_BOT_TOKEN", tg_token)
        set_data_env(data_dir, "TELEGRAM_ALLOWED_USERS", tg_users)
        say(f"{OK} configured Telegram channel (bot token + allowed users) in {Path(data_dir) / '.env'}")
        # Fresh install: Hermes starts fresh next step. Re-run: restart to pick it up.
        if out(["docker", "inspect", "-f", "{{.State.Running}}", "hermes"]) == "true":
            run(["docker", "restart", "hermes"])

    # 8. pull + up
    if not args.no_pull:
        say(f"{ARROW} pulling images…")
        run(["docker", "compose", "pull"], quiet=False)
    say(f"{ARROW} starting the stack…")
    run(["docker", "compose", "up", "-d"], quiet=False)
    wait_hermes_healthy()

    # 9. default model + probe
    run(["docker", "exec", "hermes", "hermes", "config", "set", "model.default", model])
    say(f"{OK} set model.default = {model}")
    if api_key:
        say(f"{ARROW} probing Hermes end-to-end…")
        p = run(["docker", "exec", "hermes", "hermes", "-z", "Say PONG and nothing else"], capture=True)
        if "pong" in (p.stdout + p.stderr).lower():
            say(f"{OK} Hermes answered through {args.provider} — install verified")
        else:
            say(f"{WARN} Hermes did not return PONG. Check the model id ('{model}') and key.")

    # 10. Hindsight memory
    if not args.no_memory:
        pull_hindsight_models()
        say(f"{ARROW} enabling Hindsight as Hermes's memory provider…")
        for k, v in (("memory.memory_enabled", "true"), ("memory.provider", "hindsight"),
                     ("memory.user_profile_enabled", "true"), ("memory.write_approval", "false")):
            run(["docker", "exec", "hermes", "hermes", "config", "set", k, v])
        run(["docker", "restart", "hermes"])
        wait_hermes_healthy()
        status = out(["docker", "exec", "hermes", "hermes", "memory", "status"])
        if re.search(r"Status:\s*available", status):
            say(f"{OK} Hindsight is the active memory provider and reachable")
        else:
            say(f"{WARN} Hindsight is configured but reports 'not available'. Check:")
            say("    docker exec hermes hermes memory status")
        hport = env_value("HINDSIGHT_API_PORT", "8888") or "8888"
        try:
            with urllib.request.urlopen(f"http://localhost:{hport}/health", timeout=5):
                say(f"{OK} Hindsight API healthy (http://localhost:{hport}/health)")
        except Exception:
            say(f"{WARN} Hindsight API not responding yet — check 'docker logs hindsight'.")
    else:
        say(f"{ARROW} skipping Hindsight memory wiring (--no-memory)")

    # 11. Windmill
    if not args.no_windmill:
        setup_windmill()
    else:
        say(f"{ARROW} skipping Windmill setup (--no-windmill)")

    # 12. MLX host server (opt-in; Apple Silicon only)
    if args.with_mlx:
        setup_mlx()

    print()
    say("Done. Services:")
    say("  Windmill:        http://windmill.localhost")
    say("  Hermes dash:     http://hermes.localhost")
    say("  Hindsight UI:    http://hindsight.localhost")
    say("  Headroom stats:  http://headroom.localhost/stats")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("\ninterrupted")
