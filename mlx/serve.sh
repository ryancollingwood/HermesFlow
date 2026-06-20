#!/usr/bin/env bash
# Starts mlx_lm.server natively on the macOS host (Apple Silicon only).
# Must run on the host, not in a container — Docker Desktop on Mac does not
# pass the GPU/Neural Engine through to containers, so MLX has nothing to
# accelerate on inside one. See README.md in this directory for setup.
set -euo pipefail

MODEL="${1:-${MLX_MODEL:-mlx-community/Qwen2.5-7B-Instruct-4bit}}"
PORT="${MLX_HOST_PORT:-8080}"

command -v mlx_lm.server >/dev/null 2>&1 || {
  echo "✗ mlx_lm.server not found — install with: pip install mlx-lm" >&2
  exit 1
}

echo "→ Loading $MODEL, serving on http://0.0.0.0:$PORT (Ctrl-C to stop)"
exec mlx_lm.server --model "$MODEL" --host 0.0.0.0 --port "$PORT"
