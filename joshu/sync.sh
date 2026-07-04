#!/usr/bin/env bash
# =============================================================================
#  Pull upstream joshu-oss changes, rebuild the image, restart joshu-stack.
#
#  Usage:  make joshu-sync          (or:  bash joshu/sync.sh)
#
#  Steps:
#    1. git fetch in JOSHU_SRC_DIR; abort if the working tree is dirty
#    2. git pull --ff-only (abort rather than merge if the clone has diverged)
#    3. report the deploy/RELEASE.json version + upstream-pin deltas
#    4. rebuild the image (joshu/build.sh — honours JOSHU_BUILD_NATIVE=1)
#    5. docker compose up -d joshu-stack (recreates onto the new image)
#    6. poll /joshu/api/instance/health for up to 10 minutes
#
#  Already up to date + image exists → exits early without rebuilding.
#  Force a rebuild anyway with JOSHU_FORCE_REBUILD=1.
# =============================================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
STACK_DIR="$(pwd)"

if [[ -f .env ]]; then
  env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | xargs; }
  : "${JOSHU_SRC_DIR:=$(env_get JOSHU_SRC_DIR)}"
  : "${JOSHU_IMAGE_REF:=$(env_get JOSHU_IMAGE_REF)}"
fi
JOSHU_SRC_DIR="${JOSHU_SRC_DIR:-$HOME/Projects/joshu-oss}"
JOSHU_SRC_DIR="${JOSHU_SRC_DIR/#\~/$HOME}"
JOSHU_SRC_DIR="$(eval echo "$JOSHU_SRC_DIR")"
JOSHU_IMAGE_REF="${JOSHU_IMAGE_REF:-joshu-oss:local}"

[[ -d "$JOSHU_SRC_DIR/.git" ]] || { echo "✗ $JOSHU_SRC_DIR is not a git clone"; exit 1; }

release_summary() {
  node -e "const r=require('$JOSHU_SRC_DIR/deploy/RELEASE.json');console.log(r.version+' hermes='+r.hermesRef.slice(0,8)+' gbrain='+r.gbrainRef.slice(0,8)+' camofox='+r.camofoxBase.split(':').pop().slice(0,8))"
}

echo "→ syncing $JOSHU_SRC_DIR from upstream…"
git -C "$JOSHU_SRC_DIR" fetch origin

if ! git -C "$JOSHU_SRC_DIR" diff --quiet || ! git -C "$JOSHU_SRC_DIR" diff --cached --quiet; then
  echo "✗ $JOSHU_SRC_DIR has uncommitted changes — commit or stash them first"
  exit 1
fi

BEFORE_COMMIT="$(git -C "$JOSHU_SRC_DIR" rev-parse HEAD)"
BEFORE_RELEASE="$(release_summary)"

git -C "$JOSHU_SRC_DIR" pull --ff-only

AFTER_COMMIT="$(git -C "$JOSHU_SRC_DIR" rev-parse HEAD)"
AFTER_RELEASE="$(release_summary)"

if [[ "$BEFORE_COMMIT" == "$AFTER_COMMIT" ]]; then
  echo "→ already up to date ($AFTER_RELEASE)"
  if docker image inspect "$JOSHU_IMAGE_REF" >/dev/null 2>&1 && [[ "${JOSHU_FORCE_REBUILD:-0}" != "1" ]]; then
    echo "✓ image $JOSHU_IMAGE_REF exists — nothing to do (JOSHU_FORCE_REBUILD=1 to rebuild anyway)"
    exit 0
  fi
else
  echo "→ updated $(git -C "$JOSHU_SRC_DIR" rev-list --count "$BEFORE_COMMIT..$AFTER_COMMIT") commit(s): ${BEFORE_COMMIT:0:8} → ${AFTER_COMMIT:0:8}"
  echo "  release: $BEFORE_RELEASE"
  echo "        →  $AFTER_RELEASE"
  if [[ "$BEFORE_RELEASE" != "$AFTER_RELEASE" ]]; then
    echo "  ⚠ upstream pins changed — full image rebuild required (this is the slow path)"
  fi
fi

bash "$STACK_DIR/joshu/build.sh"

if ! grep -qE '^COMPOSE_FILE=.*docker-compose\.joshu\.yml' "$STACK_DIR/.env" 2>/dev/null; then
  echo "→ joshu override not enabled in COMPOSE_FILE — image is built; run 'make joshu' to start it"
  exit 0
fi

echo "→ restarting joshu-stack on the new image…"
(cd "$STACK_DIR" && docker compose up -d joshu-stack)

echo "→ waiting for Joshu health (up to 10 min — gateway sync + GBrain index)…"
DEADLINE=$((SECONDS + 600))
until docker exec joshu-stack curl -fsS http://127.0.0.1:8788/joshu/api/instance/health >/dev/null 2>&1; do
  if (( SECONDS >= DEADLINE )); then
    echo "✗ Joshu did not become healthy within 10 minutes"
    echo "  inspect: docker logs joshu-stack --tail 100"
    exit 1
  fi
  sleep 10
done
echo "✓ Joshu is healthy — http://joshu.localhost"
