-- The Brain — webhook_secrets gains the registered workflow file path
-- PostgreSQL 16

-- workflow_file_path: the absolute path the workflow .py file lives at.
-- Recorded by `brain register-webhook` at registration time so the
-- HTTP endpoint can locate and import the workflow when an inbound
-- request arrives — mirrors the workflow_file_path column on
-- workflow_schedules (which the scheduler daemon uses the same way).
-- Nullable on the column so any pre-existing rows survive the migration.
-- CLI-side enforcement in src/cli.py register-webhook always populates
-- the column for new registrations, so freshly-registered webhooks
-- always carry a path.
ALTER TABLE webhook_secrets
    ADD COLUMN workflow_file_path TEXT;
