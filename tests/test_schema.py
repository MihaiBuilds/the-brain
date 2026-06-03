"""Tests for the M2 schema migration (``002_schedules_and_state.sql``).

This PR is schema-only — no Python code is changed yet. The point of these
tests is to confirm the new tables, columns, indexes, and constraints land
correctly when the migration runner applies the file. Behavioral tests for
the daemon, lifecycle CLI, state placeholder, and so on live in follow-up
PRs alongside the code that uses these tables.
"""

from __future__ import annotations

import os

import psycopg
import pytest

DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


def _columns(table: str) -> dict[str, dict]:
    """Return a {column_name: {data_type, is_nullable, column_default}} map."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = %s
            """,
            (table,),
        )
        return {
            row[0]: {"data_type": row[1], "is_nullable": row[2], "default": row[3]}
            for row in cur.fetchall()
        }


def _index_defs(table: str) -> dict[str, str]:
    """Return a {index_name: indexdef} map for the table."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
            (table,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# workflow_runs gains two nullable columns and one partial index
#
# (Migration-applied tracking via the ``_migrations`` table is a property of
# the production migration runner — tests bypass it for speed by applying
# the SQL files directly. End-to-end verification of ``_migrations`` happens
# via ``brain status`` against the real container, not in this test file.)
# ---------------------------------------------------------------------------


def test_workflow_runs_has_previous_run_id_column():
    cols = _columns("workflow_runs")
    assert "previous_run_id" in cols
    assert cols["previous_run_id"]["data_type"] == "uuid"
    assert cols["previous_run_id"]["is_nullable"] == "YES"


def test_workflow_runs_has_planned_steps_jsonb_column():
    cols = _columns("workflow_runs")
    assert "planned_steps" in cols
    assert cols["planned_steps"]["data_type"] == "jsonb"
    assert cols["planned_steps"]["is_nullable"] == "YES"


def test_workflow_runs_previous_run_id_self_foreign_key():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT confrelid::regclass::text
              FROM pg_constraint
             WHERE conname = 'workflow_runs_previous_run_id_fkey'
            """
        )
        result = cur.fetchone()
    assert result is not None, "FK constraint missing"
    assert result[0] == "workflow_runs"


def test_workflow_runs_has_partial_index_for_previous_lookup():
    indexes = _index_defs("workflow_runs")
    assert "workflow_runs_name_previous_idx" in indexes
    indexdef = indexes["workflow_runs_name_previous_idx"]
    assert "workflow_name" in indexdef
    assert "started_at DESC" in indexdef
    assert "status = 'success'" in indexdef.replace('"', "")


# ---------------------------------------------------------------------------
# workflow_schedules table
# ---------------------------------------------------------------------------


def test_workflow_schedules_table_exists_with_expected_columns():
    cols = _columns("workflow_schedules")
    expected = {
        "id": "uuid",
        "workflow_name": "text",
        "workflow_file_path": "text",
        "cron_expression": "text",
        "enabled": "boolean",
        "last_run_id": "uuid",
        "next_run_at": "timestamp with time zone",
        "created_at": "timestamp with time zone",
    }
    assert set(cols) == set(expected)
    for name, data_type in expected.items():
        assert cols[name]["data_type"] == data_type


def test_workflow_schedules_workflow_name_is_unique():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO workflow_schedules (workflow_name, workflow_file_path, cron_expression) "
            "VALUES ('dup', 'a.py', '0 * * * *')"
        )
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO workflow_schedules (workflow_name, workflow_file_path, cron_expression) "
                    "VALUES ('dup', 'b.py', '0 9 * * *')"
                )
                conn.commit()
                pytest.fail("expected UniqueViolation")
            except psycopg.errors.UniqueViolation:
                pass


def test_workflow_schedules_enabled_defaults_to_true():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_schedules (workflow_name, workflow_file_path, cron_expression) "
            "VALUES ('with_default', 'd.py', '0 9 * * *') RETURNING enabled"
        )
        (enabled,) = cur.fetchone()
    assert enabled is True


def test_workflow_schedules_last_run_id_foreign_key_to_workflow_runs():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT confrelid::regclass::text
              FROM pg_constraint
             WHERE conname = 'workflow_schedules_last_run_id_fkey'
            """
        )
        result = cur.fetchone()
    assert result is not None, "FK constraint missing"
    assert result[0] == "workflow_runs"


def test_workflow_schedules_has_partial_index_on_enabled_next_run_at():
    indexes = _index_defs("workflow_schedules")
    assert "workflow_schedules_enabled_next_run_at_idx" in indexes
    indexdef = indexes["workflow_schedules_enabled_next_run_at_idx"]
    assert "next_run_at" in indexdef
    assert "enabled = true" in indexdef


# ---------------------------------------------------------------------------
# daemon_heartbeats table
# ---------------------------------------------------------------------------


def test_daemon_heartbeats_table_exists_with_expected_columns():
    cols = _columns("daemon_heartbeats")
    expected = {
        "daemon_id": "text",
        "started_at": "timestamp with time zone",
        "last_tick_at": "timestamp with time zone",
    }
    assert set(cols) == set(expected)
    for name, data_type in expected.items():
        assert cols[name]["data_type"] == data_type
    for col in expected:
        assert cols[col]["is_nullable"] == "NO"


def test_daemon_heartbeats_daemon_id_is_primary_key():
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
              FROM pg_index i
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = 'daemon_heartbeats'::regclass AND i.indisprimary
            """
        )
        pk_columns = [row[0] for row in cur.fetchall()]
    assert pk_columns == ["daemon_id"]
