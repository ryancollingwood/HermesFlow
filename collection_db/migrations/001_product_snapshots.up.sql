BEGIN;

CREATE TABLE IF NOT EXISTS collection.hermesflow_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collection.product_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    execution_trace_id UUID NOT NULL,
    source_trace_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    source_content_hash CHAR(64) NOT NULL
        CHECK (source_content_hash ~ '^[0-9a-f]{64}$'),
    normalized_product_id CHAR(64) NOT NULL
        CHECK (normalized_product_id ~ '^[0-9a-f]{64}$'),
    source_product_id CHAR(64) NOT NULL
        CHECK (source_product_id ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    product_payload JSONB NOT NULL,
    payload_hash CHAR(64) NOT NULL
        CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT product_snapshots_execution_source_product_key UNIQUE (
        execution_trace_id,
        source_artifact_id,
        normalized_product_id
    )
);

CREATE INDEX IF NOT EXISTS product_snapshots_source_artifact_idx
    ON collection.product_snapshots (source_artifact_id);
CREATE INDEX IF NOT EXISTS product_snapshots_source_trace_idx
    ON collection.product_snapshots (source_trace_id);

COMMENT ON TABLE collection.product_snapshots IS
    'Versioned HF-024 normalized product snapshots with HF-018 execution and artifact lineage.';

INSERT INTO collection.hermesflow_schema_migrations (version)
VALUES ('001_product_snapshots')
ON CONFLICT (version) DO NOTHING;

COMMIT;
