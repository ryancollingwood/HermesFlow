#!/usr/bin/env bash
# =============================================================================
#  migrate-hindsight-db.sh
#  Migrates Hindsight data from the embedded pg0 database to the external
#  hindsight_db container introduced in the label-backup integration.
#
#  Run in two stages:
#
#    Stage 1 — DUMP   (old stack still running, before switching compose files)
#      ./scripts/migrate-hindsight-db.sh dump
#
#    Stage 2 — RESTORE (new stack configured, hindsight_db container available)
#      ./scripts/migrate-hindsight-db.sh restore
#      # or point at a specific dump:
#      BACKUP_FILE=backups/hindsight-pg0-2025-01-01-0200.sql.gz \
#        ./scripts/migrate-hindsight-db.sh restore
#
#  The pg0 embedded PostgreSQL binary lives at a fixed path inside the
#  Hindsight container and is NOT on PATH — this script uses the absolute path.
#  pg0 ships PostgreSQL 18 with pgvector; hindsight_db uses pgvector/pgvector:pg16.
#  The SQL dump format is cross-version compatible for typical application data.
# =============================================================================

set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
PG_DUMP_BIN="/home/hindsight/.pg0/installation/18.1.0/bin/pg_dump"
PG0_PORT=5555
PG0_USER=hindsight
PG0_DB=hindsight

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

load_env() {
  if [[ -f .env ]]; then
    set -a; source .env; set +a
  fi
}

die() { echo "✗ $*" >&2; exit 1; }

wait_healthy() {
  local service="$1" max="${2:-60}" i
  echo "→ Waiting for $service to be healthy..."
  for i in $(seq 1 "$max"); do
    if $COMPOSE exec -T "$service" pg_isready -U hindsight -q 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  die "$service did not become healthy after $((max * 2))s."
}

# ---------------------------------------------------------------------------
#  Stage 1: dump from embedded pg0
# ---------------------------------------------------------------------------

cmd_dump() {
  load_env

  local stamp; stamp=$(date +%F-%H%M)
  local out="${BACKUP_FILE:-backups/hindsight-pg0-${stamp}.sql.gz}"

  mkdir -p "$(dirname "$out")"

  # Verify old container is running
  if ! docker ps --format '{{.Names}}' | grep -qx 'hindsight'; then
    die "'hindsight' container is not running.\n  Start it with the old docker-compose.yml before running the dump."
  fi

  # Verify pg_dump binary exists at the expected path
  if ! docker exec hindsight test -x "$PG_DUMP_BIN" 2>/dev/null; then
    # pg0 version may differ — try to locate it
    local found
    found=$(docker exec hindsight find /home/hindsight/.pg0/installation -name pg_dump -type f 2>/dev/null | head -1)
    if [[ -z "$found" ]]; then
      die "pg_dump not found inside the Hindsight container at $PG_DUMP_BIN.\n  Run: docker exec hindsight find /home/hindsight/.pg0 -name pg_dump\n  Then set PG_DUMP_BIN=<path> and re-run."
    fi
    echo "  (pg_dump found at $found — using that instead of expected path)"
    PG_DUMP_BIN="$found"
  fi

  echo "→ Dumping embedded pg0 database from the Hindsight container..."
  docker exec hindsight \
    "$PG_DUMP_BIN" \
    -h localhost -p "$PG0_PORT" \
    -U "$PG0_USER" -d "$PG0_DB" \
    --no-owner --no-acl \
    | gzip > "$out"

  echo "✓ Dump saved to $out"
  echo
  echo "Next steps:"
  echo "  1. Switch to the updated docker-compose.yml (git pull)."
  echo "  2. Run: BACKUP_FILE=$out ./scripts/migrate-hindsight-db.sh restore"
}

# ---------------------------------------------------------------------------
#  Stage 2: restore into hindsight_db
# ---------------------------------------------------------------------------

cmd_restore() {
  load_env

  # Resolve backup file
  local src="${BACKUP_FILE:-}"
  if [[ -z "$src" ]]; then
    src=$(ls -t backups/hindsight-pg0-*.sql.gz 2>/dev/null | head -1 || true)
    [[ -n "$src" ]] || die "No backup file found in ./backups/. Run the dump stage first or set BACKUP_FILE=<path>."
    echo "→ Using most recent dump: $src"
  fi
  [[ -f "$src" ]] || die "Backup file not found: $src"

  # Bring up hindsight_db (idempotent)
  echo "→ Starting hindsight_db..."
  $COMPOSE up -d hindsight_db

  wait_healthy hindsight_db

  echo "→ Restoring $src into hindsight_db..."
  gunzip -c "$src" | $COMPOSE exec -T hindsight_db \
    psql -U hindsight -d hindsight -v ON_ERROR_STOP=1

  echo "✓ Restore complete."
  echo
  echo "Start Hindsight: $COMPOSE up -d hindsight"
}

# ---------------------------------------------------------------------------
#  Dispatch
# ---------------------------------------------------------------------------

case "${1:-}" in
  dump)    cmd_dump ;;
  restore) cmd_restore ;;
  *)
    echo "Usage: $0 <command>"
    echo
    echo "Commands:"
    echo "  dump     Export data from the embedded pg0 (run while old Hindsight container is running)"
    echo "  restore  Import into hindsight_db (run after switching to the updated docker-compose.yml)"
    echo
    echo "Environment variables:"
    echo "  BACKUP_FILE   Path to the .sql.gz dump (restore stage; auto-detected if omitted)"
    echo "  PG_DUMP_BIN   Absolute path to pg_dump inside the Hindsight container (dump stage)"
    echo "  COMPOSE       Docker Compose command (default: 'docker compose')"
    exit 1
    ;;
esac
