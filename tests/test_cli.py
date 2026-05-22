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
