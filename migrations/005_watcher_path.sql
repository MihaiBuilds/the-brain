-- The Brain — file_watchers gains the registered workflow file path
-- PostgreSQL 16

-- workflow_file_path: the absolute path the workflow .py file lives at.
-- Recorded by `brain register-watcher` at registration time so the
-- watcher daemon can locate and import the workflow when a filesystem
-- event arrives — mirrors the workflow_file_path column on
-- workflow_schedules (cron) and webhook_secrets (HTTP webhooks).
-- Nullable on the column so any pre-existing rows survive the migration.
-- CLI-side enforcement in src/cli.py register-watcher always populates
-- the column for new registrations, so freshly-registered watchers
-- always carry a path.
ALTER TABLE file_watchers
    ADD COLUMN workflow_file_path TEXT;
