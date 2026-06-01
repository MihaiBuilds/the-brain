"""Tests for the schedule lifecycle CLI commands.

Drives ``brain register/list/disable/enable/unregister`` through click's
``CliRunner``. The commands are synchronous — each one calls
``asyncio.run`` internally and opens/closes its own pool — so these tests
are plain synchronous functions and never touch the session-scoped async
pool from ``conftest.py``.

Schedule rows are seeded with a synchronous psycopg insert for assertions
about behaviors that don't need to round-trip through register itself.
"""

import os
from datetime import UTC, datetime

import psycopg
from click.testing import CliRunner

from src.cli import cli

_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


def _write_workflow(tmp_path, name, workflow_name=None):
    """Write a minimal valid workflow file and return its path."""
    wf_name = workflow_name or name.removesuffix(".py")
    path = tmp_path / name
    path.write_text(
        "from src.workflow import Workflow, ShellStep\n"
        f"workflow = Workflow(name='{wf_name}', steps=[ShellStep(name='s', command='echo hi')])\n"
    )
    return str(path)


def _seed_schedule(name, cron="*/5 * * * *", enabled=True, file_path=None):
    """Insert one schedule row directly and return nothing."""
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO workflow_schedules
                (workflow_name, workflow_file_path, cron_expression, enabled, next_run_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (name, file_path or f"/tmp/{name}.py", cron, enabled, datetime.now(UTC)),
        )


def _fetch_schedule(name):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        cur = conn.execute(
            "SELECT * FROM workflow_schedules WHERE workflow_name = %s",
            (name,),
        )
        cur.row_factory = psycopg.rows.dict_row
        return cur.fetchone()


# ---------------------------------------------------------------------------
# brain register
# ---------------------------------------------------------------------------


def test_register_inserts_a_row_with_absolute_path_and_next_fire(tmp_path):
    path = _write_workflow(tmp_path, "ok.py")
    result = CliRunner().invoke(cli, ["register", path, "--cron", "*/5 * * * *"])

    assert result.exit_code == 0, result.output
    assert "Registered 'ok'" in result.output

    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.row_factory = psycopg.rows.dict_row
        row = conn.execute("SELECT * FROM workflow_schedules WHERE workflow_name = 'ok'").fetchone()

    assert row is not None
    assert row["cron_expression"] == "*/5 * * * *"
    assert row["enabled"] is True
    assert row["workflow_file_path"].startswith("/")  # absolute
    assert row["workflow_file_path"].endswith("ok.py")
    assert row["next_run_at"] is not None
    assert row["next_run_at"] > datetime.now(UTC)


def test_register_uses_name_override_when_provided(tmp_path):
    path = _write_workflow(tmp_path, "ok.py", workflow_name="original")
    result = CliRunner().invoke(
        cli, ["register", path, "--cron", "*/5 * * * *", "--name", "renamed"]
    )

    assert result.exit_code == 0, result.output
    assert "Registered 'renamed'" in result.output

    with psycopg.connect(_DSN, autocommit=True) as conn:
        cur = conn.execute(
            "SELECT workflow_name FROM workflow_schedules WHERE workflow_name = 'renamed'"
        )
        assert cur.fetchone() is not None


def test_register_rejects_invalid_cron(tmp_path):
    path = _write_workflow(tmp_path, "ok.py")
    result = CliRunner().invoke(cli, ["register", path, "--cron", "not a cron"])

    assert result.exit_code == 1
    assert "Error" in result.output

    with psycopg.connect(_DSN, autocommit=True) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM workflow_schedules")
        assert cur.fetchone()[0] == 0


def test_register_rejects_unloadable_workflow_file(tmp_path):
    missing = str(tmp_path / "ghost.py")
    result = CliRunner().invoke(cli, ["register", missing, "--cron", "*/5 * * * *"])

    assert result.exit_code == 1
    assert "Error" in result.output


def test_register_rejects_duplicate_name(tmp_path):
    path = _write_workflow(tmp_path, "dup.py")
    first = CliRunner().invoke(cli, ["register", path, "--cron", "*/5 * * * *"])
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(cli, ["register", path, "--cron", "*/10 * * * *"])
    assert second.exit_code == 1
    assert "already exists" in second.output

    with psycopg.connect(_DSN, autocommit=True) as conn:
        cur = conn.execute(
            "SELECT cron_expression FROM workflow_schedules WHERE workflow_name = 'dup'"
        )
        # The original row is untouched — no silent overwrite.
        assert cur.fetchone()[0] == "*/5 * * * *"


# ---------------------------------------------------------------------------
# brain list
# ---------------------------------------------------------------------------


def test_list_empty_reports_no_schedules():
    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "No schedules" in result.output


def test_list_shows_all_columns_for_each_row():
    _seed_schedule("alpha", cron="0 9 * * 1-5", enabled=True)
    _seed_schedule("bravo", cron="*/15 * * * *", enabled=False)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "bravo" in result.output
    assert "0 9 * * 1-5" in result.output
    assert "*/15 * * * *" in result.output
    # Header line is present.
    assert "NAME" in result.output and "CRON" in result.output and "NEXT FIRE" in result.output


def test_list_filter_enabled_shows_only_enabled():
    _seed_schedule("on", enabled=True)
    _seed_schedule("off", enabled=False)

    result = CliRunner().invoke(cli, ["list", "--enabled"])
    assert result.exit_code == 0
    assert "on" in result.output
    assert "off" not in result.output


def test_list_filter_disabled_shows_only_disabled():
    _seed_schedule("on", enabled=True)
    _seed_schedule("off", enabled=False)

    result = CliRunner().invoke(cli, ["list", "--disabled"])
    assert result.exit_code == 0
    assert "off" in result.output
    assert "on" not in result.output.split("\n", 1)[1]


def test_list_filter_workflow_shows_only_named():
    _seed_schedule("alpha")
    _seed_schedule("bravo")

    result = CliRunner().invoke(cli, ["list", "--workflow", "alpha"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "bravo" not in result.output


def test_list_enabled_and_disabled_together_is_rejected():
    result = CliRunner().invoke(cli, ["list", "--enabled", "--disabled"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# brain disable / enable — idempotent UPDATE
# ---------------------------------------------------------------------------


def test_disable_flips_enabled_to_false():
    _seed_schedule("foo", enabled=True)
    result = CliRunner().invoke(cli, ["disable", "foo"])

    assert result.exit_code == 0, result.output
    assert "disabled" in result.output
    assert _fetch_schedule("foo")["enabled"] is False


def test_disable_is_idempotent_on_already_disabled():
    _seed_schedule("foo", enabled=False)
    result = CliRunner().invoke(cli, ["disable", "foo"])

    assert result.exit_code == 0
    assert _fetch_schedule("foo")["enabled"] is False


def test_enable_flips_enabled_to_true():
    _seed_schedule("foo", enabled=False)
    result = CliRunner().invoke(cli, ["enable", "foo"])

    assert result.exit_code == 0, result.output
    assert "enabled" in result.output
    assert _fetch_schedule("foo")["enabled"] is True


def test_enable_is_idempotent_on_already_enabled():
    _seed_schedule("foo", enabled=True)
    result = CliRunner().invoke(cli, ["enable", "foo"])

    assert result.exit_code == 0
    assert _fetch_schedule("foo")["enabled"] is True


def test_disable_rejects_unknown_name():
    result = CliRunner().invoke(cli, ["disable", "ghost"])
    assert result.exit_code == 1
    assert "no schedule named" in result.output


def test_enable_rejects_unknown_name():
    result = CliRunner().invoke(cli, ["enable", "ghost"])
    assert result.exit_code == 1
    assert "no schedule named" in result.output


# ---------------------------------------------------------------------------
# brain unregister — hard DELETE
# ---------------------------------------------------------------------------


def test_unregister_deletes_the_row():
    _seed_schedule("foo")
    result = CliRunner().invoke(cli, ["unregister", "foo"])

    assert result.exit_code == 0, result.output
    assert "Unregistered" in result.output
    assert _fetch_schedule("foo") is None


def test_unregister_rejects_unknown_name():
    result = CliRunner().invoke(cli, ["unregister", "ghost"])
    assert result.exit_code == 1
    assert "no schedule named" in result.output
