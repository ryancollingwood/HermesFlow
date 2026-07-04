#!/usr/bin/env bash
# =============================================================================
#  Build the joshu-oss image from a local clone (there is no public image).
#
#  Usage:  make joshu-build          (or:  bash joshu/build.sh)
#
#  Reads from .env (or the environment):
#    JOSHU_SRC_DIR      local joshu-oss clone   (default: ~/Projects/joshu-oss)
#    JOSHU_IMAGE_REF    tag to build            (default: joshu-oss:local)
#    JOSHU_BUILD_NATIVE=1  build for the host arch with a plain `docker build`
#                          instead of upstream's amd64-forced buildx script.
#                          Skips the voice-realtime image. Use on Apple Silicon
#                          — works only if the pinned camofox base image has an
#                          arm64 manifest; if the build fails resolving it,
#                          fall back to the default (emulated) path.
#
#  The default path runs upstream's `npm run vps:predeploy && vps:build-image`,
#  which pins hermes-agent / camofox / gbrain from deploy/RELEASE.json and
#  forces --platform linux/amd64 (emulated on ARM hosts — slow first build,
#  20-30+ min even natively).
# =============================================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Pull defaults from .env without clobbering values already in the environment.
if [[ -f .env ]]; then
  env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | xargs; }
  : "${JOSHU_SRC_DIR:=$(env_get JOSHU_SRC_DIR)}"
  : "${JOSHU_IMAGE_REF:=$(env_get JOSHU_IMAGE_REF)}"
fi
JOSHU_SRC_DIR="${JOSHU_SRC_DIR:-$HOME/Projects/joshu-oss}"
JOSHU_SRC_DIR="${JOSHU_SRC_DIR/#\~/$HOME}"
JOSHU_SRC_DIR="$(eval echo "$JOSHU_SRC_DIR")"   # expand ${HOME} from .env
JOSHU_IMAGE_REF="${JOSHU_IMAGE_REF:-joshu-oss:local}"

[[ -f "$JOSHU_SRC_DIR/deploy/Dockerfile" && -f "$JOSHU_SRC_DIR/deploy/RELEASE.json" ]] || {
  echo "✗ $JOSHU_SRC_DIR doesn't look like a joshu-oss clone (deploy/Dockerfile missing)"
  echo "  git clone https://github.com/db-aeon/joshu-oss \"$JOSHU_SRC_DIR\""
  echo "  or point JOSHU_SRC_DIR in .env at your clone"
  exit 1
}
command -v node >/dev/null || { echo "✗ node not found — joshu-oss needs Node 20+ to build"; exit 1; }
command -v npm  >/dev/null || { echo "✗ npm not found"; exit 1; }
# The vendored excalidraw fork installs its deps with yarn (classic).
command -v yarn >/dev/null || command -v corepack >/dev/null || {
  echo "✗ yarn (or corepack) not found — the excalidraw vendor build needs it"
  echo "  npm install -g yarn"
  exit 1
}

cd "$JOSHU_SRC_DIR"
echo "→ building $JOSHU_IMAGE_REF from $JOSHU_SRC_DIR"
echo "  upstream pin: $(node -e "const r=require('./deploy/RELEASE.json');console.log(r.version+' (hermes '+r.hermesRef.slice(0,8)+', gbrain '+r.gbrainRef.slice(0,8)+')')")"

# AGPL checkouts omit vendor/ (arozos, excalidraw) — upstream's script fetches
# the pinned trees. Fall back to plain submodules if the script ever goes away.
if [[ -x scripts/ensure-vendor-for-build.sh ]]; then
  bash scripts/ensure-vendor-for-build.sh
elif [[ -f .gitmodules ]] && git submodule status --recursive 2>/dev/null | grep -q '^-'; then
  echo "→ initializing git submodules (first build)…"
  git submodule update --init --recursive
fi

if [[ ! -d node_modules ]]; then
  echo "→ npm ci (first build)…"
  npm ci
fi

if [[ "${JOSHU_BUILD_NATIVE:-0}" == "1" ]]; then
  # Native-arch build: same pins as upstream, plain docker build, no voice image.
  echo "→ native build (host arch, no --platform linux/amd64)"
  npm run vps:predeploy
  HERMES_AGENT_REF="$(node -e "console.log(require('./deploy/RELEASE.json').hermesRef)")"
  CAMOFOX_BASE="$(node -e "console.log(require('./deploy/RELEASE.json').camofoxBase)")"
  GBRAIN_REF="$(node -e "console.log(require('./deploy/RELEASE.json').gbrainRef)")"
  docker build \
    -f deploy/Dockerfile \
    --build-arg "HERMES_AGENT_REF=$HERMES_AGENT_REF" \
    --build-arg "CAMOFOX_BASE=$CAMOFOX_BASE" \
    --build-arg "GBRAIN_REF=$GBRAIN_REF" \
    -t "$JOSHU_IMAGE_REF" \
    .
else
  # Upstream path: builds sandbox + voice images, forces linux/amd64.
  npm run vps:predeploy
  JOSHU_IMAGE_REF="$JOSHU_IMAGE_REF" npm run vps:build-image
fi

# ── runtime-deps fixup ────────────────────────────────────────────────────────
# Upstream skew (present at 0.1.32): dist/agUiApi.js imports @joshu/app-sdk,
# but deploy/runtime/package.json doesn't ship it, so the Joshu API
# crash-loops on boot (ERR_MODULE_NOT_FOUND). Layer the built package into
# the image; harmless once upstream adds it to the runtime deps.
if [[ -f packages/app-sdk/dist/index.js ]]; then
  IMG_ARCH="$(docker image inspect --format '{{.Architecture}}' "$JOSHU_IMAGE_REF")"
  docker build -q --platform "linux/$IMG_ARCH" -t "$JOSHU_IMAGE_REF" -f - . <<EOF >/dev/null
FROM $JOSHU_IMAGE_REF
COPY packages/app-sdk/package.json /opt/joshu/node_modules/@joshu/app-sdk/package.json
COPY packages/app-sdk/dist /opt/joshu/node_modules/@joshu/app-sdk/dist
EOF
  echo "→ layered @joshu/app-sdk into the image (upstream runtime-deps skew fixup)"
fi

echo "✓ built $JOSHU_IMAGE_REF"
docker image inspect --format '  size: {{.Size}} bytes, arch: {{.Architecture}}' "$JOSHU_IMAGE_REF" 2>/dev/null || true
echo "  next: make joshu"
