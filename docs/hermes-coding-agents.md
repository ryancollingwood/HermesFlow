# Coding agent CLIs in the Hermes container (claude-code, opencode, codex)

Hermes ships bundled skills that orchestrate three external coding-agent CLIs
via its `terminal()` tool:
[`claude-code`](https://code.claude.com/docs/en/cli-reference),
[`opencode`](https://opencode.ai), and
[`codex`](https://github.com/openai/codex)
(`/opt/hermes/skills/autonomous-ai-agents/{claude-code,opencode,codex}/SKILL.md`
inside the image). This page covers what's baked into the image, what isn't,
and how to authenticate each one.

## What's baked in

[`hermes/Dockerfile`](Dockerfile) installs `tmux` (apt) and the
`@anthropic-ai/claude-code` / `opencode-ai` npm packages at build time, as
**system/npm tools** — separate from the Python `requirements.txt` /
`LAZY_DEPS` mechanism described in [README.md](README.md), since these aren't
Python packages and don't touch `/opt/hermes/.venv`. `codex` (`@openai/codex`)
is **not** baked in;
its skill exists in the image, but the binary doesn't, since no Hermes use
case has needed it yet. To add it, append `@openai/codex` to the `npm install
-g` line in the Dockerfile and rebuild.

| Tool | Binary baked in? | Install command (if adding) |
|---|---|---|
| `tmux` | Yes | `apt-get install -y tmux` |
| `claude` (claude-code) | Yes | `npm install -g @anthropic-ai/claude-code` |
| `opencode` | Yes | `npm install -g opencode-ai@latest` |
| `codex` | No | `npm install -g @openai/codex` |

## Why tmux matters

The `claude-code` skill's "Interactive PTY" mode — the only reliable way to
drive Claude Code's TUI for multi-turn sessions — depends on
`tmux new-session` / `send-keys` / `capture-pane`. `opencode` and `codex` use
Hermes's own `pty=true` terminal mode instead and don't require tmux, but it's
installed unconditionally since claude-code needs it.

## Auth (not baked in, by design)

None of the three CLIs come pre-authenticated — credentials are set up
per-deployment, manually, after the container is running:

- **claude-code**: run `claude` once inside the container for browser OAuth
  (Pro/Max), or `claude auth login --console` for API-key billing, or set
  `ANTHROPIC_API_KEY`. Config lands under `$CLAUDE_CONFIG_DIR`
  (`/opt/data/.claude`, set explicitly in `docker-compose.yml` even though
  `HOME=/opt/data` for the `hermes` user would already resolve there).
- **opencode**: run `opencode auth login` inside the container, or set a
  provider env var (e.g. `OPENROUTER_API_KEY`). Config lands at
  `$OPENCODE_CONFIG` (`/opt/data/.config/opencode/opencode.json`, set
  explicitly in `docker-compose.yml`, same rationale as `CLAUDE_CONFIG_DIR` —
  `HOME=/opt/data` already places the default XDG path there, but pinning it
  is defensive against any future change to opencode's resolution logic).
- **codex**: set `OPENAI_API_KEY`, or use the Codex CLI's own OAuth login
  flow. Credentials land under `$CODEX_HOME` (`/opt/data/.codex`, set
  explicitly in `docker-compose.yml` ahead of the binary actually being baked
  in — see the table above). Separately, Hermes itself can use Codex OAuth via
  `hermes auth add openai-codex` (`model.provider: openai-codex`) — that's
  Hermes-managed and independent of the standalone CLI's own auth.

Because `/opt/data` is the bind-mounted, persistent volume (already used for
Hermes's own config/sessions/skills), auth state for all three CLIs survives
container rebuilds and restarts as long as it's written under `$CLAUDE_CONFIG_DIR`
/ `$OPENCODE_CONFIG` / `$CODEX_HOME` (all under `/opt/data`).

Example, run via `docker exec -it hermes <cmd>` or through Hermes's own
`terminal()` tool:

```bash
docker exec -it hermes claude                       # OAuth login
docker exec -it hermes opencode auth login           # provider login
docker exec hermes sh -c 'opencode auth list'        # verify
```

## Verifying the setup

```bash
docker exec hermes which tmux claude opencode
docker exec hermes claude --version
docker exec hermes opencode --version
docker exec hermes sh -c 'echo $CLAUDE_CONFIG_DIR'   # -> /opt/data/.claude
docker exec hermes sh -c 'echo $OPENCODE_CONFIG'     # -> /opt/data/.config/opencode/opencode.json
docker exec hermes sh -c 'echo $CODEX_HOME'          # -> /opt/data/.codex
```

These installs live in system/npm paths, not the Python venv, so they're
unaffected by — and don't need — `make hermes-heal` (which only targets
venv-overlay drift; see
[README.md: Troubleshooting](README.md#troubleshooting-stray-package-overlays-hermes-heal)).
