-- The Brain — Initial Schema
-- PostgreSQL 16

-- workflow_runs — one row per workflow execution.
-- Every run is logged with its status, output, and any error.
CREATE TABLE workflow_runs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name      TEXT NOT NULL,
    workflow_file_path TEXT NOT NULL,
    started_at         TIMESTAMPTZ NOT NULL,
    ended_at           TIMESTAMPTZ,
    status             TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    output             JSONB,
    error              TEXT,
    created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX workflow_runs_name_idx    ON workflow_runs (workflow_name);
CREATE INDEX workflow_runs_status_idx  ON workflow_runs (status);
CREATE INDEX workflow_runs_started_idx ON workflow_runs (started_at DESC);
