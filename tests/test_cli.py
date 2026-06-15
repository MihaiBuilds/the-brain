"""Tests for the ``brain`` command-line interface.

The commands are driven through click's ``CliRunner``. Each command is
synchronous — it calls ``asyncio.run`` internally and opens/closes its own
connection pool — so these tests are plain synchronous functions and never
touch the session-scoped async pool from ``conftest.py``.

``brain run`` is exercised with a real shell workflow written to a temp
file. ``history`` and ``show`` are checked against runs seeded with a
synchronous psycopg insert, keeping this module fully off the async loop.
"""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from click.testing import CliRunner

from src.cli import cli

_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


def _write_workflow(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def _seed_run(workflow_name, status="success"):
    """Insert one finished run directly and return its UUID."""
    run_id = uuid4()
    now = datetime.now(UTC)
    output = [{"name": "s", "success": status == "success", "output": "hi"}]
    error = None if status == "success" else "step 's' failed: boom"
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs
                (id, workflow_name, workflow_file_path, started_at,
                 ended_at, status, output, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                workflow_name,
                f"{workflow_name}.py",
                now,
                now,
                status,
                json.dumps(output),
                error,
            ),
        )
    return run_id


# ---------------------------------------------------------------------------
# brain run
# ---------------------------------------------------------------------------


def test_run_exits_zero_on_success(tmp_path):
    path = _write_workflow(
        tmp_path,
        "ok.py",
        "from src.workflow import Workflow, ShellStep\n"
        "workflow = Workflow(name='ok', steps=[ShellStep(name='s', command='echo hi')])\n",
    )
    result = CliRunner().invoke(cli, ["run", path])
    assert result.exit_code == 0, result.output
    assert "✓ s" in result.output
    assert "success" in result.output


def test_run_exits_one_on_step_failure(tmp_path):
    path = _write_workflow(
        tmp_path,
        "bad.py",
        "from src.workflow import Workflow, ShellStep\n"
        "workflow = Workflow(name='bad', steps=[ShellStep(name='s', command='exit 1')])\n",
    )
    result = CliRunner().invoke(cli, ["run", path])
    assert result.exit_code == 1
    assert "✗ s" in result.output


def test_run_exits_one_on_unloadable_file(tmp_path):
    result = CliRunner().invoke(cli, ["run", str(tmp_path / "missing.py")])
    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# brain history
# ---------------------------------------------------------------------------


def test_history_empty_reports_no_runs():
    result = CliRunner().invoke(cli, ["history"])
    assert result.exit_code == 0
    assert "No runs found" in result.output


def test_history_lists_seeded_runs():
    _seed_run("alpha")
    _seed_run("bravo")
    result = CliRunner().invoke(cli, ["history"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "bravo" in result.output


def test_history_filters_by_workflow_name():
    _seed_run("keep")
    _seed_run("drop")
    result = CliRunner().invoke(cli, ["history", "--workflow", "keep"])
    assert result.exit_code == 0
    assert "keep" in result.output
    assert "drop" not in result.output


def test_history_filters_by_status():
    _seed_run("good", status="success")
    _seed_run("bad", status="failed")
    result = CliRunner().invoke(cli, ["history", "--status", "failed"])
    assert result.exit_code == 0
    assert "bad" in result.output
    assert "good" not in result.output


# ---------------------------------------------------------------------------
# brain show
# ---------------------------------------------------------------------------


def test_show_displays_full_run_detail():
    run_id = _seed_run("detailed")
    result = CliRunner().invoke(cli, ["show", str(run_id)])
    assert result.exit_code == 0
    assert "detailed" in result.output
    assert "success" in result.output
    assert "✓ s" in result.output


def test_show_accepts_a_short_id_prefix():
    run_id = _seed_run("prefixed")
    result = CliRunner().invoke(cli, ["show", str(run_id)[:8]])
    assert result.exit_code == 0
    assert "prefixed" in result.output


def test_show_unknown_id_exits_one():
    result = CliRunner().invoke(cli, ["show", "ffffffff"])
    assert result.exit_code == 1
    assert "no run matching" in result.output


def test_show_failed_run_shows_the_error():
    run_id = _seed_run("broke", status="failed")
    result = CliRunner().invoke(cli, ["show", str(run_id)])
    assert result.exit_code == 0
    assert "failed" in result.output
    assert "✗ s" in result.output


def test_show_rejects_sql_like_wildcards():
    """`brain show abc%` must reject the wildcard rather than passing it through
    to a LIKE query — otherwise a malicious or accidental wildcard returns
    runs the user did not ask for."""
    result = CliRunner().invoke(cli, ["show", "abc%"])
    assert result.exit_code == 1
    assert "hex digits and hyphens" in result.output


def test_show_rejects_sql_like_underscore():
    """SQL LIKE treats `_` as a single-char wildcard. Reject it too."""
    result = CliRunner().invoke(cli, ["show", "abc_def"])
    assert result.exit_code == 1
    assert "hex digits and hyphens" in result.output


# ---------------------------------------------------------------------------
# brain --version
# ---------------------------------------------------------------------------


def test_version_flag_prints_semver_shape():
    """`brain --version` must exit 0 and print something semver-shaped so
    bug reports can include the version."""
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    # click.version_option emits `brain, version X.Y.Z` by default.
    assert "version" in result.output.lower()
    assert any(char.isdigit() for char in result.output)


# ---------------------------------------------------------------------------
# brain migrate
# ---------------------------------------------------------------------------


def test_migrate_reports_zero_when_schema_up_to_date():
    """`brain migrate` must clearly report when no new migrations ran, so
    the user knows the call was a no-op rather than mistaking the silent
    success of the old `Migrations complete.` line for "everything ran".

    Setup: pre-populate the ``_migrations`` tracking table with every known
    migration file so `brain migrate` perceives the schema as up to date.
    The conftest applies the SQL outside the tracking table, so without
    this step the CLI would try (and fail) to re-apply existing relations.
    """
    from pathlib import Path

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    migration_names = sorted(p.name for p in migrations_dir.glob("*.sql"))
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        for name in migration_names:
            conn.execute(
                "INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
                (name,),
            )

    result = CliRunner().invoke(cli, ["migrate"])
    assert result.exit_code == 0
    assert "already up to date" in result.output


# ---------------------------------------------------------------------------
# brain status — unhealthy must exit 1 (Docker healthcheck contract)
# ---------------------------------------------------------------------------


def test_status_exits_one_when_database_unreachable(monkeypatch):
    """`brain status` is used as a Docker healthcheck. When the DB is down it
    must exit non-zero — otherwise healthchecks silently report healthy on
    a dead service.

    The pool is set at import time from env, so we patch ``health_check``
    directly to simulate an unreachable DB without disrupting the session
    pool used by other tests in this module.
    """
    import src.db as db_module

    async def fake_health_check():
        return {"status": "unhealthy", "error": "connection refused"}

    monkeypatch.setattr(db_module, "health_check", fake_health_check)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 1
    assert "unhealthy" in result.output.lower()


# ---------------------------------------------------------------------------
# brain serve — verify the startup error names THE_BRAIN_API_TOKEN
# (regression lock; behavior already correct as of M5 audit)
# ---------------------------------------------------------------------------


def test_serve_startup_error_names_the_env_var(monkeypatch):
    """When `THE_BRAIN_API_TOKEN` is unset, the startup error message must
    name the env var explicitly so the user knows what to set. Currently
    correct; this test locks it against regression."""
    monkeypatch.delenv("THE_BRAIN_API_TOKEN", raising=False)
    result = CliRunner().invoke(cli, ["serve"])
    assert result.exit_code == 1
    assert "THE_BRAIN_API_TOKEN" in result.output


# ---------------------------------------------------------------------------
# Truncation indicator (history / list / list-triggers)
# ---------------------------------------------------------------------------


def test_history_truncates_long_workflow_name_with_ellipsis():
    """Workflow names longer than the column width (23 chars) must be
    truncated with `…` so a 30-char name and the same name + `-v2` are
    visually distinguishable."""
    _seed_run("a-very-long-workflow-name-that-exceeds-the-column")
    result = CliRunner().invoke(cli, ["history"])
    assert result.exit_code == 0
    assert "…" in result.output


# ---------------------------------------------------------------------------
# brain diagnose — bundle a redacted snapshot for bug reports
# ---------------------------------------------------------------------------


def test_diagnose_creates_zip_in_current_directory(tmp_path, monkeypatch):
    """`brain diagnose` writes a zip to the current directory whose name
    matches the locked timestamped pattern, and exits 0."""
    import os
    import re

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["diagnose"])
    assert result.exit_code == 0, result.output

    written = [f for f in os.listdir(tmp_path) if f.startswith("brain-diagnostic-")]
    assert len(written) == 1
    assert re.match(r"^brain-diagnostic-\d{4}-\d{2}-\d{2}-\d{6}\.zip$", written[0])

    # Output names the file path so the user can find it.
    assert "Diagnostic bundle written" in result.output
    assert written[0] in result.output
