BEGIN;

DROP TABLE IF EXISTS collection.product_snapshots;
DELETE FROM collection.hermesflow_schema_migrations
WHERE version = '001_product_snapshots';

COMMIT;
