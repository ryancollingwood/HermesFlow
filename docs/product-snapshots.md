# Product snapshot persistence

HF-025 persists HF-024 normalized products in
`collection.product_snapshots`. Each row is unique for the execution trace,
source artifact, and normalized product. Replaying the same key reuses that row
instead of creating a duplicate: a changed payload updates it, while a
byte-identical replay is a true no-op reported as `unchanged`. A different
execution intentionally creates a new historical snapshot.

The table keeps queryable lineage columns for the execution trace, source trace,
source artifact ID and hash, plus the complete normalized product JSON. The
canonical JSON SHA-256 is stored as `payload_hash` so downstream comparisons can
avoid reparsing unchanged records.

## Applying migrations

Fresh databases apply every `collection_db/migrations/*.up.sql` file from the
Postgres init hook. The hook only runs for an empty data directory. Existing
installations apply the same idempotent files explicitly:

```bash
make collection-db-migrate
```

Migration `001_product_snapshots` includes a matching `.down.sql` file for
disposable-environment and rollback verification. Do not run down migrations
against production data without an approved recovery plan.

## Previewing a write

Set `preview=true` when running
`f/capabilities/collection/product_snapshot_write`. Preview validates the
normalization result and execution context, computes stable payload hashes and
returns the planned keys. It exits before opening a database connection.
