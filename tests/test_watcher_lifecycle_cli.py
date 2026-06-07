"""Tests for the file watcher trigger lifecycle CLI commands.

Drives ``brain register-watcher / disable-watcher / enable-watcher /
unregister-watcher`` through click's ``CliRunner``. Synchronous tests
— each command runs its own ``asyncio.run`` internally, so these tests
do not touch the session async pool from ``conftest.py``.

Watcher rows are seeded with a synchronous psycopg insert for
behaviors that do not need to round-trip through register-watcher
itself.
"""

import json
import os

import psycopg
from click.testing import CliRunner

from src.cli import cli

_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


def _write_workflow(tmp_path, name, workflow_name=None):
    wf_name = workflow_name or name.removesuffix(".py")
    path = tmp_path / name
    path.write_text(
        "from src.workflow import Workflow, ShellStep\n"
        f"workflow = Workflow(name='{wf_name}', steps=[ShellStep(name='s', command='echo hi')])\n"
    )
    return str(path)


def _seed_watcher(name, watched_path="/tmp/watched", events=None, enabled=True, file_path=None):
    events_json = json.dumps(events or ["modified"])
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO file_watchers "
            "(workflow_name, watched_path, watched_events, enabled, workflow_file_path) "
            "VALUES (%s, %s, %s::jsonb, %s, %s)",
            (name, watched_path, events_json, enabled, file_path or f"/tmp/{name}.py"),
        )


def _fetch_watcher(name):
    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_name, watched_path, watched_events, enabled, workflow_file_path "
            "FROM file_watchers WHERE workflow_name = %s",
            (name,),
        )
        row = cur.fetchone()
    return row


# ---------------------------------------------------------------------------
# register-watcher
# ---------------------------------------------------------------------------


def test_register_watcher_happy_path(tmp_path):
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "wf.py")
    result = CliRunner().invoke(
        cli,
        ["register-watcher", workflow_path, "--path", str(watch_dir), "--events", "modified"],
    )
    assert result.exit_code == 0, result.output
    assert "Registered watcher 'wf'" in result.output

    row = _fetch_watcher("wf")
    assert row is not None
    # Absolute path resolution.
    assert row[1] == str(watch_dir.resolve())
    assert row[2] == ["modified"]
    assert row[3] is True
    assert row[4] == str((tmp_path / "wf.py").resolve())


def test_register_watcher_defaults_events_to_modified(tmp_path):
    watch_dir = tmp_path / "dwatch"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "def_events.py", workflow_name="def_events")
    result = CliRunner().invoke(cli, ["register-watcher", workflow_path, "--path", str(watch_dir)])
    assert result.exit_code == 0, result.output
    assert _fetch_watcher("def_events")[2] == ["modified"]


def test_register_watcher_accepts_multiple_events(tmp_path):
    watch_dir = tmp_path / "multi"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "multi.py")
    result = CliRunner().invoke(
        cli,
        [
            "register-watcher",
            workflow_path,
            "--path",
            str(watch_dir),
            "--events",
            "created,modified,deleted",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _fetch_watcher("multi")[2] == ["created", "modified", "deleted"]


def test_register_watcher_rejects_nonexistent_path(tmp_path):
    workflow_path = _write_workflow(tmp_path, "ghost.py")
    result = CliRunner().invoke(
        cli,
        ["register-watcher", workflow_path, "--path", str(tmp_path / "missing_dir")],
    )
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert _fetch_watcher("ghost") is None


def test_register_watcher_rejects_non_directory_path(tmp_path):
    """A file (not a directory) is rejected at registration time."""
    workflow_path = _write_workflow(tmp_path, "notdir.py")
    file_target = tmp_path / "a_file.txt"
    file_target.write_text("hi")
    result = CliRunner().invoke(
        cli,
        ["register-watcher", workflow_path, "--path", str(file_target)],
    )
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_register_watcher_rejects_unknown_event_name(tmp_path):
    watch_dir = tmp_path / "bad"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "bad_ev.py")
    result = CliRunner().invoke(
        cli,
        [
            "register-watcher",
            workflow_path,
            "--path",
            str(watch_dir),
            "--events",
            "modified,renamed",
        ],
    )
    assert result.exit_code != 0
    assert "unknown event" in result.output


def test_register_watcher_rejects_empty_events_list(tmp_path):
    watch_dir = tmp_path / "empty"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "empty.py")
    result = CliRunner().invoke(
        cli,
        ["register-watcher", workflow_path, "--path", str(watch_dir), "--events", ","],
    )
    assert result.exit_code != 0
    assert "non-empty" in result.output


def test_register_watcher_rejects_duplicate_name(tmp_path):
    watch_dir = tmp_path / "dup"
    watch_dir.mkdir()
    _seed_watcher("dup", watched_path=str(watch_dir))
    workflow_path = _write_workflow(tmp_path, "dup.py")
    result = CliRunner().invoke(cli, ["register-watcher", workflow_path, "--path", str(watch_dir)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_register_watcher_supports_name_override(tmp_path):
    watch_dir = tmp_path / "rn"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "wf.py", workflow_name="wf")
    result = CliRunner().invoke(
        cli,
        [
            "register-watcher",
            workflow_path,
            "--path",
            str(watch_dir),
            "--name",
            "renamed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _fetch_watcher("renamed") is not None
    assert _fetch_watcher("wf") is None


def test_register_watcher_rejects_unknown_workflow_file(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    result = CliRunner().invoke(
        cli,
        [
            "register-watcher",
            str(tmp_path / "does_not_exist.py"),
            "--path",
            str(watch_dir),
        ],
    )
    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# disable / enable / unregister
# ---------------------------------------------------------------------------


def test_disable_watcher_toggles_enabled_flag():
    _seed_watcher("toggle")
    result = CliRunner().invoke(cli, ["disable-watcher", "toggle"])
    assert result.exit_code == 0
    assert _fetch_watcher("toggle")[3] is False


def test_enable_watcher_toggles_back():
    _seed_watcher("toggle", enabled=False)
    result = CliRunner().invoke(cli, ["enable-watcher", "toggle"])
    assert result.exit_code == 0
    assert _fetch_watcher("toggle")[3] is True


def test_disable_watcher_is_idempotent():
    _seed_watcher("off", enabled=False)
    result = CliRunner().invoke(cli, ["disable-watcher", "off"])
    assert result.exit_code == 0


def test_unregister_watcher_hard_deletes_the_row():
    _seed_watcher("byebye")
    result = CliRunner().invoke(cli, ["unregister-watcher", "byebye"])
    assert result.exit_code == 0
    assert _fetch_watcher("byebye") is None


def test_unregister_watcher_unknown_name_fails():
    result = CliRunner().invoke(cli, ["unregister-watcher", "ghost"])
    assert result.exit_code == 1
    assert "no watcher named" in result.output


# ---------------------------------------------------------------------------
# list-triggers shows watchers alongside cron + webhooks
# ---------------------------------------------------------------------------


def test_list_triggers_includes_watchers_in_unified_view():
    _seed_watcher("w1", watched_path="/data/in")
    result = CliRunner().invoke(cli, ["list-triggers"])
    assert result.exit_code == 0
    assert "file" in result.output
    assert "w1" in result.output
    assert "/data/in" in result.output


def test_list_triggers_shows_all_three_types_when_present(tmp_path):
    """Registering a watcher via the CLI lands it in list-triggers."""
    watch_dir = tmp_path / "all"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "all.py")
    CliRunner().invoke(cli, ["register-watcher", workflow_path, "--path", str(watch_dir)])

    # Insert a fake schedule + webhook directly to keep this test focused.
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO workflow_schedules "
            "(workflow_name, workflow_file_path, cron_expression) "
            "VALUES ('s1', '/tmp/s1.py', '0 0 * * *')"
        )
        conn.execute(
            "INSERT INTO webhook_secrets "
            "(workflow_name, hmac_secret, workflow_file_path) "
            "VALUES ('h1', 'sec', '/tmp/h1.py')"
        )

    result = CliRunner().invoke(cli, ["list-triggers"])
    assert result.exit_code == 0
    assert "cron" in result.output
    assert "webhook" in result.output
    assert "file" in result.output
    assert "all" in result.output
