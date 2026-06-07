"""Tests for the webhook trigger lifecycle CLI commands.

Drives ``brain register-webhook / disable-webhook / enable-webhook /
unregister-webhook / list-triggers`` through click's ``CliRunner``. The
commands are synchronous — each one calls ``asyncio.run`` internally
and opens/closes its own pool — so these tests are plain synchronous
functions and never touch the session-scoped async pool from
``conftest.py``.

Webhook rows are seeded with a synchronous psycopg insert for
assertions about behaviors that don't need to round-trip through
register-webhook itself.
"""

import os

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


def _seed_webhook(name, hmac_secret="seeded-secret", enabled=True, file_path=None):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO webhook_secrets "
            "(workflow_name, hmac_secret, enabled, workflow_file_path) "
            "VALUES (%s, %s, %s, %s)",
            (name, hmac_secret, enabled, file_path or f"/tmp/{name}.py"),
        )


def _seed_schedule(name, cron="*/5 * * * *", enabled=True):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO workflow_schedules "
            "(workflow_name, workflow_file_path, cron_expression, enabled) "
            "VALUES (%s, %s, %s, %s)",
            (name, f"/tmp/{name}.py", cron, enabled),
        )


def _seed_watcher(name, watched_path="/tmp/watched", enabled=True):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO file_watchers "
            "(workflow_name, watched_path, watched_events, enabled) "
            "VALUES (%s, %s, %s::jsonb, %s)",
            (name, watched_path, '["modified"]', enabled),
        )


def _fetch_webhook(name):
    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_name, hmac_secret, enabled, workflow_file_path "
            "FROM webhook_secrets WHERE workflow_name = %s",
            (name,),
        )
        row = cur.fetchone()
    return row


# ---------------------------------------------------------------------------
# register-webhook
# ---------------------------------------------------------------------------


def test_register_webhook_happy_path(tmp_path):
    workflow_path = _write_workflow(tmp_path, "ping.py")
    result = CliRunner().invoke(cli, ["register-webhook", workflow_path])
    assert result.exit_code == 0, result.output
    assert "Registered webhook 'ping'." in result.output
    assert "Save this secret now" in result.output
    assert "cannot be retrieved later" in result.output


def test_register_webhook_prints_a_secret_that_round_trips_into_the_db(tmp_path):
    workflow_path = _write_workflow(tmp_path, "round.py")
    result = CliRunner().invoke(cli, ["register-webhook", workflow_path])
    assert result.exit_code == 0
    # The secret line is preceded by two spaces and surrounded by blanks;
    # extract it via the unique indent.
    secret_line = next(
        ln for ln in result.output.splitlines() if ln.startswith("  ") and ln.strip()
    )
    printed_secret = secret_line.strip()
    row = _fetch_webhook("round")
    assert row is not None
    assert row[1] == printed_secret
    assert row[2] is True


def test_register_webhook_rejects_unknown_workflow_file_before_insert(tmp_path):
    missing = tmp_path / "does-not-exist.py"
    result = CliRunner().invoke(cli, ["register-webhook", str(missing)])
    assert result.exit_code == 1
    assert "Error" in result.output
    # Nothing landed in the DB.
    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM webhook_secrets")
        (count,) = cur.fetchone()
    assert count == 0


def test_register_webhook_rejects_duplicate_name_before_insert(tmp_path):
    workflow_path = _write_workflow(tmp_path, "dup.py")
    _seed_webhook("dup")
    result = CliRunner().invoke(cli, ["register-webhook", workflow_path])
    assert result.exit_code == 1
    assert "already exists" in result.output
    # Original row unchanged.
    row = _fetch_webhook("dup")
    assert row[1] == "seeded-secret"


def test_register_webhook_records_absolute_workflow_file_path(tmp_path):
    """The workflow file path is stored as absolute so the daemon's CWD does not matter."""
    workflow_path = _write_workflow(tmp_path, "abs.py")
    result = CliRunner().invoke(cli, ["register-webhook", workflow_path])
    assert result.exit_code == 0, result.output
    row = _fetch_webhook("abs")
    assert row is not None
    # Column index 3 is workflow_file_path per the _fetch_webhook SELECT order.
    assert row[3] == str(tmp_path.resolve() / "abs.py")


def test_register_webhook_supports_name_override(tmp_path):
    workflow_path = _write_workflow(tmp_path, "wf.py", workflow_name="wf")
    result = CliRunner().invoke(cli, ["register-webhook", workflow_path, "--name", "renamed"])
    assert result.exit_code == 0
    assert _fetch_webhook("renamed") is not None
    assert _fetch_webhook("wf") is None


# ---------------------------------------------------------------------------
# disable-webhook / enable-webhook
# ---------------------------------------------------------------------------


def test_disable_webhook_toggles_enabled_flag():
    _seed_webhook("toggle")
    result = CliRunner().invoke(cli, ["disable-webhook", "toggle"])
    assert result.exit_code == 0
    assert _fetch_webhook("toggle")[2] is False


def test_enable_webhook_toggles_enabled_flag_back():
    _seed_webhook("toggle", enabled=False)
    result = CliRunner().invoke(cli, ["enable-webhook", "toggle"])
    assert result.exit_code == 0
    assert _fetch_webhook("toggle")[2] is True


def test_disable_webhook_is_idempotent():
    """Disabling an already-disabled webhook succeeds without error."""
    _seed_webhook("already-off", enabled=False)
    result = CliRunner().invoke(cli, ["disable-webhook", "already-off"])
    assert result.exit_code == 0
    assert _fetch_webhook("already-off")[2] is False


def test_enable_webhook_is_idempotent():
    """Enabling an already-enabled webhook succeeds without error."""
    _seed_webhook("already-on", enabled=True)
    result = CliRunner().invoke(cli, ["enable-webhook", "already-on"])
    assert result.exit_code == 0
    assert _fetch_webhook("already-on")[2] is True


def test_disable_webhook_fails_on_unknown_name():
    result = CliRunner().invoke(cli, ["disable-webhook", "ghost"])
    assert result.exit_code == 1
    assert "no webhook named" in result.output


# ---------------------------------------------------------------------------
# unregister-webhook
# ---------------------------------------------------------------------------


def test_unregister_webhook_deletes_the_row():
    _seed_webhook("to-delete")
    result = CliRunner().invoke(cli, ["unregister-webhook", "to-delete"])
    assert result.exit_code == 0
    assert _fetch_webhook("to-delete") is None


def test_unregister_webhook_fails_on_unknown_name():
    result = CliRunner().invoke(cli, ["unregister-webhook", "ghost"])
    assert result.exit_code == 1
    assert "no webhook named" in result.output


def test_unregister_webhook_preserves_past_workflow_runs():
    """Hard-delete of the webhook does not cascade into workflow_runs."""
    _seed_webhook("with-runs")
    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_runs "
            "(workflow_name, workflow_file_path, status, started_at, output) "
            "VALUES (%s, %s, %s, now(), '[]'::jsonb) RETURNING id",
            ("with-runs", "/tmp/with-runs.py", "success"),
        )
        (run_id,) = cur.fetchone()

    result = CliRunner().invoke(cli, ["unregister-webhook", "with-runs"])
    assert result.exit_code == 0
    assert _fetch_webhook("with-runs") is None

    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM workflow_runs WHERE id = %s", (run_id,))
        assert cur.fetchone() is not None, "past run was unexpectedly deleted"


# ---------------------------------------------------------------------------
# list-triggers
# ---------------------------------------------------------------------------


def test_list_triggers_says_none_when_all_three_tables_are_empty():
    result = CliRunner().invoke(cli, ["list-triggers"])
    assert result.exit_code == 0
    assert "No triggers registered." in result.output


def test_list_triggers_shows_cron_schedules_only_when_only_schedules_exist():
    _seed_schedule("nightly", cron="0 0 * * *")
    result = CliRunner().invoke(cli, ["list-triggers"])
    assert result.exit_code == 0
    assert "cron" in result.output
    assert "nightly" in result.output
    assert "0 0 * * *" in result.output
    assert "webhook" not in result.output
    assert "file" not in result.output


def test_list_triggers_shows_all_three_trigger_types_when_present():
    _seed_schedule("sched", cron="*/5 * * * *")
    _seed_webhook("hook")
    _seed_watcher("watch", watched_path="/data/in")
    result = CliRunner().invoke(cli, ["list-triggers"])
    assert result.exit_code == 0
    assert "cron" in result.output
    assert "webhook" in result.output
    assert "file" in result.output
    assert "sched" in result.output
    assert "hook" in result.output
    assert "watch" in result.output
    assert "/data/in" in result.output


def test_list_triggers_shows_disabled_rows_with_enabled_no():
    _seed_schedule("off-cron", enabled=False)
    _seed_webhook("off-hook", enabled=False)
    result = CliRunner().invoke(cli, ["list-triggers"])
    assert result.exit_code == 0
    # Each line shows ENABLED column; "no" must appear at least twice.
    assert result.output.count("no") >= 2
