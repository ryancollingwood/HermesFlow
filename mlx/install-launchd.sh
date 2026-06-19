#!/usr/bin/env bash
# Installs and starts mlx_lm.server as a launchd user agent — it'll start at
# login and restart automatically if it crashes. Run on the macOS host (not
# in a container). See README.md for the manual-start alternative.
set -euo pipefail

LABEL="com.hermesflow.mlx"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/HermesFlow"

MLX_MODEL="${MLX_MODEL:-mlx-community/Qwen2.5-7B-Instruct-4bit}"
MLX_HOST_PORT="${MLX_HOST_PORT:-8080}"
MLX_VENV_BIN="${MLX_VENV_BIN:-$HOME/.mlx-venv/bin}"

if [[ ! -x "$MLX_VENV_BIN/mlx_lm.server" ]]; then
  echo "✗ mlx_lm.server not found at $MLX_VENV_BIN/mlx_lm.server" >&2
  echo "  Install it first (see README.md: pip install mlx-lm), or set" >&2
  echo "  MLX_VENV_BIN=/path/to/venv/bin if it lives somewhere else." >&2
  exit 1
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"

sed \
  -e "s#__MLX_VENV_BIN__#$MLX_VENV_BIN#g" \
  -e "s#__MLX_MODEL__#$MLX_MODEL#g" \
  -e "s#__MLX_PORT__#$MLX_HOST_PORT#g" \
  -e "s#__LOG_DIR__#$LOG_DIR#g" \
  -e "s#__HOME__#$HOME#g" \
  "$SCRIPT_DIR/com.hermesflow.mlx.plist.template" > "$PLIST_PATH"

# Idempotent: tear down a previous instance (if any) before loading the new one.
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/$LABEL"

echo "✓ Installed and started $LABEL"
echo "  Model:  $MLX_MODEL"
echo "  Port:   $MLX_HOST_PORT"
echo "  Plist:  $PLIST_PATH"
echo "  Logs:   $LOG_DIR/mlx.out.log / mlx.err.log"
echo "  Status: launchctl print gui/$(id -u)/$LABEL"
echo "  Remove: mlx/uninstall-launchd.sh"
