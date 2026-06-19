#!/usr/bin/env bash
# Stops and removes the launchd user agent installed by install-launchd.sh.
set -euo pipefail

LABEL="com.hermesflow.mlx"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -f "$PLIST_PATH" ]]; then
  echo "→ $LABEL is not installed (no plist at $PLIST_PATH)"
  exit 0
fi

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"
echo "✓ Stopped and removed $LABEL"
