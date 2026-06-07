-- The Brain — Triggers (Webhooks + File Watchers) Schema
-- PostgreSQL 16

-- webhook_secrets — one row per registered webhook trigger.
-- Each webhook has its own HMAC-SHA256 secret. The webhook endpoint
-- looks up the secret by workflow_name and verifies the incoming
-- X-Brain-Signature header constant-time before firing the workflow.
-- No FK to workflow_schedules: webhooks register independently from
-- cron schedules; the UNIQUE constraint on workflow_name is the only
-- uniqueness guarantee.
CREATE TABLE webhook_secrets (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name TEXT NOT NULL UNIQUE,
    hmac_secret   TEXT NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- file_watchers — one row per registered file watcher trigger.
-- The watcher daemon reads enabled rows, spawns one watchdog Observer
-- per row, and fires the workflow when filesystem events matching
-- watched_events occur in watched_path. Single directory per watcher,
-- no recursion (v1.0 scope).
CREATE TABLE file_watchers (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name  TEXT NOT NULL UNIQUE,
    watched_path   TEXT NOT NULL,
    watched_events JSONB NOT NULL,
    enabled        BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ DEFAULT now()
);

-- workflow_runs gains one new column:
--   trigger_context — JSONB snapshot of the inbound payload when the
--                     run was invoked by a webhook or file watcher.
--                     NULL for manual (`brain run`) and cron-triggered
--                     runs. Powers the {trigger.X} placeholder family
--                     (body, headers, event, path).
ALTER TABLE workflow_runs
    ADD COLUMN trigger_context JSONB;
