#!/bin/bash
# Runs once, on first init of an empty collection_db data dir (standard
# postgres-image /docker-entrypoint-initdb.d behaviour — re-running the
# container against an existing volume does NOT re-run this).
#
# Creates three isolated surfaces in one physical database:
#   - baserow    schema: Baserow's own private app schema. No other role gets
#                access — Baserow's table/field DDL is internally managed and
#                direct external writes risk corrupting it. Any Baserow <->
#                collection sync goes through Baserow webhooks, not SQL.
#   - directus   schema: Directus's own system tables (directus_users, etc).
#   - collection schema: shared business data (page scrapes, LLM generations,
#                triage records) — writable by directus and the windmill
#                collection role, NOT by baserow.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE ROLE baserow LOGIN PASSWORD '${BASEROW_DB_PASSWORD}';
  CREATE SCHEMA IF NOT EXISTS baserow AUTHORIZATION baserow;
  ALTER ROLE baserow SET search_path TO baserow, public;

  CREATE ROLE directus LOGIN PASSWORD '${DIRECTUS_DB_PASSWORD}';
  CREATE SCHEMA IF NOT EXISTS directus AUTHORIZATION directus;
  ALTER ROLE directus SET search_path TO directus, collection, public;

  CREATE ROLE windmill_collection LOGIN PASSWORD '${WINDMILL_COLLECTION_DB_PASSWORD}';
  ALTER ROLE windmill_collection SET search_path TO collection, public;

  CREATE SCHEMA IF NOT EXISTS collection;
  GRANT USAGE, CREATE ON SCHEMA collection TO directus, windmill_collection;
  ALTER DEFAULT PRIVILEGES IN SCHEMA collection GRANT ALL ON TABLES TO directus, windmill_collection;
  ALTER DEFAULT PRIVILEGES IN SCHEMA collection GRANT ALL ON SEQUENCES TO directus, windmill_collection;
EOSQL
