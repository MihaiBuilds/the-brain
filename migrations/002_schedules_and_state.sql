-- The Brain — Triggers + State Schema
-- PostgreSQL 16

-- workflow_schedules — one row per registered workflow schedule.
-- The scheduler daemon reads from this table, fires due workflows,
-- and updates last_run_id + next_run_at after each fire.
CREATE TABLE workflow_schedules (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name      TEXT NOT NULL UNIQUE,
    workflow_file_path TEXT NOT NULL,
    cron_expression    TEXT NOT NULL,
    enabled            BOOLEAN NOT NULL DEFAULT true,
    last_run_id        UUID REFERENCES workflow_runs(id),
    next_run_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now()
);

-- Partial index: the daemon's "what's due" query only cares about enabled rows
-- ordered by next_run_at. Disabled rows are skipped.
CREATE INDEX workflow_schedules_enabled_next_run_at_idx
    ON workflow_schedules (next_run_at)
    WHERE enabled = true;

-- daemon_heartbeats — single-row-per-daemon liveness table.
-- The daemon writes its last_tick_at on every poll cycle; the healthcheck
-- reads this to determine whether the daemon is alive.
CREATE TABLE daemon_heartbeats (
    daemon_id    TEXT PRIMARY KEY,
    started_at   TIMESTAMPTZ NOT NULL,
    last_tick_at TIMESTAMPTZ NOT NULL
);

-- workflow_runs gains two new columns:
--   previous_run_id — link to the previous successful run of the same
--                     workflow, supports the {previous.step_name} placeholder.
--   planned_steps   — JSONB snapshot of the workflow's step list at
--                     run-creation time. Lets postmortem disambiguate
--                     "step never ran because the workflow halted" from
--                     "step was removed from the workflow definition since."
ALTER TABLE workflow_runs
    ADD COLUMN previous_run_id UUID REFERENCES workflow_runs(id),
    ADD COLUMN planned_steps   JSONB;

-- Partial index supporting the {previous.step_name} lookup:
-- find the most recent successful run for a given workflow_name.
CREATE INDEX workflow_runs_name_previous_idx
    ON workflow_runs (workflow_name, started_at DESC)
    WHERE status = 'success';
