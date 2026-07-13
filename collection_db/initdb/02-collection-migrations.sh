#!/bin/bash
# Apply version-controlled collection schema migrations on first database init.
# Existing installations use `make collection-db-migrate` instead because the
# Postgres image only runs /docker-entrypoint-initdb.d for an empty data dir.
set -euo pipefail

for migration in /collection-migrations/*.up.sql; do
  [ -f "$migration" ] || continue
  psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --file "$migration"
done
