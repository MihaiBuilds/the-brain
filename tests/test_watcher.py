"""Tests for the file watcher daemon (``src.triggers.watcher``).

These tests exercise the daemon against the real filesystem — events
come from real file writes/creates/deletes in pytest's ``tmp_path``.
The watchdog Observer runs on its own thread and schedules into the
asyncio loop via ``run_coroutine_threadsafe``, so each test polls
briefly for the side-effect (a row in ``workflow_runs``) to appear.

The in-memory debounce state and the global observer pool are reset
between tests via the module-level helpers in ``src.triggers.watcher``
so each test starts from a clean slate.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import psycopg
import pytest

from src.db import execute_query, fetch_all, fetch_one
from src.runner import run_workflow
from src.triggers.watcher import (
    _recover_orphans,
    _reset_debounce_for_tests,
    _reset_pool_for_tests,
    _should_fire,
    watcher_daemon_id,
    watcher_tick,
)
from src.workflow.models import ShellStep, Workflow

_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


@pytest.fixture(autouse=True)
def _reset_watcher_state():
    """Clear in-memory debounce + observer pool between tests.

    Synchronous on purpose — the CLI tests do not request ``db_pool``
    and must not share an asyncio loop with one. Async tests that need
    a pool request ``db_pool`` explicitly.
    """
    _reset_debounce_for_tests()
    _reset_pool_for_tests()
    yield
    _reset_pool_for_tests()
    _reset_debounce_for_tests()


def _write_workflow(tmp_path, name, workflow_name=None, command="echo hi"):
    wf_name = workflow_name or name.removesuffix(".py")
    path = tmp_path / name
    path.write_text(
        "from src.workflow import Workflow, ShellStep\n"
        f"workflow = Workflow(name='{wf_name}', steps=[ShellStep(name='s', command='{command}')])\n"
    )
    return str(path)


def _seed_watcher(name, watched_path, *, events=None, enabled=True):
    import json as _json

    events_json = _json.dumps(events or ["modified"])
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO file_watchers "
            "(workflow_name, watched_path, watched_events, enabled) "
            "VALUES (%s, %s, %s::jsonb, %s)",
            (name, watched_path, events_json, enabled),
        )


async def _seed_recent_run_with_path(workflow_name: str, file_path: str) -> None:
    """Insert a successful run so the watcher's path resolver finds the file."""
    await execute_query(
        "INSERT INTO workflow_runs "
        "(workflow_name, workflow_file_path, status, started_at, ended_at, output) "
        "VALUES (%s, %s, 'success', now(), now(), '[]'::jsonb)",
        (workflow_name, file_path),
    )


async def _wait_for_run(workflow_name: str, *, timeout_s: float = 3.0) -> dict | None:
    """Poll workflow_runs until a row for this workflow appears, or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        rows = await fetch_all(
            "SELECT id, trigger_context FROM workflow_runs "
            "WHERE workflow_name = %s AND trigger_context IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (workflow_name,),
        )
        if rows:
            return rows[0]
        await asyncio.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# Debounce — pure-function tests (no FS needed)
# ---------------------------------------------------------------------------


def test_debounce_first_event_always_fires():
    _reset_debounce_for_tests()
    assert _should_fire("wf", "/path/a", now=1.0) is True


def test_debounce_event_within_window_does_not_fire():
    from src.triggers.watcher import _record_fire

    _reset_debounce_for_tests()
    _record_fire("wf", "/path/a", 1.0)
    # 499ms later (within 500ms window) — must not fire.
    assert _should_fire("wf", "/path/a", now=1.499) is False


def test_debounce_event_after_window_fires():
    from src.triggers.watcher import _record_fire

    _reset_debounce_for_tests()
    _record_fire("wf", "/path/a", 1.0)
    # 501ms later (just outside 500ms window) — must fire.
    assert _should_fire("wf", "/path/a", now=1.501) is True


def test_debounce_is_scoped_per_workflow_and_path():
    from src.triggers.watcher import _record_fire

    _reset_debounce_for_tests()
    _record_fire("wf-a", "/path/a", 1.0)
    # Different workflow, same path — fires immediately.
    assert _should_fire("wf-b", "/path/a", now=1.1) is True
    # Same workflow, different path — fires immediately.
    assert _should_fire("wf-a", "/path/b", now=1.1) is True


# ---------------------------------------------------------------------------
# watcher_tick — observer setup against real FS events
# ---------------------------------------------------------------------------


async def test_watcher_tick_observes_modify_event_and_fires_workflow(tmp_path, db_pool):
    """A file write inside the watched directory fires the workflow."""
    watch_dir = tmp_path / "watch_mod"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "fire_mod.py", workflow_name="fire_mod")
    _seed_watcher("fire_mod", str(watch_dir), events=["modified"])
    await _seed_recent_run_with_path("fire_mod", workflow_path)

    await watcher_tick(datetime.now(UTC))

    # Trigger a modify event.
    target = watch_dir / "file.txt"
    target.write_text("initial")
    # Modify after a brief delay so it shows up as 'modified' on macOS.
    await asyncio.sleep(0.15)
    target.write_text("changed")

    row = await _wait_for_run("fire_mod")
    assert row is not None
    assert row["trigger_context"]["event"] == "file"
    assert row["trigger_context"]["path"].endswith("file.txt")


async def test_watcher_tick_filters_event_types_per_row(tmp_path, db_pool):
    """A watcher configured for 'created' only does NOT fire on 'modified'."""
    watch_dir = tmp_path / "watch_created"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "only_created.py", workflow_name="only_created")
    _seed_watcher("only_created", str(watch_dir), events=["created"])
    await _seed_recent_run_with_path("only_created", workflow_path)

    await watcher_tick(datetime.now(UTC))

    # Create first — this MUST fire.
    target = watch_dir / "a.txt"
    target.write_text("hello")
    row = await _wait_for_run("only_created", timeout_s=2.0)
    assert row is not None, "created event should have fired"

    # Reset debounce so the next event can fire even if events come fast.
    _reset_debounce_for_tests()

    # Wipe rows we just observed so we can assert the next event does NOT fire.
    await execute_query(
        "DELETE FROM workflow_runs WHERE workflow_name = %s",
        ("only_created",),
    )
    await _seed_recent_run_with_path("only_created", workflow_path)

    # Modify the same file — this MUST NOT fire (events filter is created-only).
    await asyncio.sleep(0.2)
    target.write_text("changed")

    # Wait briefly for any spurious event to land, then assert none did.
    await asyncio.sleep(0.5)
    rows = await fetch_all(
        "SELECT id FROM workflow_runs WHERE workflow_name = %s AND trigger_context IS NOT NULL",
        ("only_created",),
    )
    assert rows == []


async def test_watcher_tick_routes_event_to_correct_workflow(tmp_path, db_pool):
    """Events in dir A fire workflow A, events in dir B fire workflow B."""
    dir_a = tmp_path / "dir_a"
    dir_a.mkdir()
    dir_b = tmp_path / "dir_b"
    dir_b.mkdir()
    wf_a = _write_workflow(tmp_path, "wf_a.py", workflow_name="wf_a")
    wf_b = _write_workflow(tmp_path, "wf_b.py", workflow_name="wf_b")
    _seed_watcher("wf_a", str(dir_a))
    _seed_watcher("wf_b", str(dir_b))
    await _seed_recent_run_with_path("wf_a", wf_a)
    await _seed_recent_run_with_path("wf_b", wf_b)

    await watcher_tick(datetime.now(UTC))

    target_a = dir_a / "x.txt"
    target_a.write_text("a")
    await asyncio.sleep(0.15)
    target_a.write_text("a-changed")

    row_a = await _wait_for_run("wf_a")
    assert row_a is not None
    assert "dir_a" in row_a["trigger_context"]["path"]

    row_b = await _wait_for_run("wf_b", timeout_s=0.4)
    assert row_b is None, "wf_b should not have fired"


async def test_watcher_tick_skips_disabled_watchers(tmp_path, db_pool):
    """A disabled watcher row is not picked up by sync."""
    watch_dir = tmp_path / "watch_off"
    watch_dir.mkdir()
    workflow_path = _write_workflow(tmp_path, "off.py", workflow_name="off_wf")
    _seed_watcher("off_wf", str(watch_dir), enabled=False)
    await _seed_recent_run_with_path("off_wf", workflow_path)

    await watcher_tick(datetime.now(UTC))

    (watch_dir / "x.txt").write_text("ignored")
    await asyncio.sleep(0.3)

    rows = await fetch_all(
        "SELECT id FROM workflow_runs WHERE workflow_name = %s AND trigger_context IS NOT NULL",
        ("off_wf",),
    )
    assert rows == []


async def test_watcher_tick_skips_watcher_pointing_at_nonexistent_directory(tmp_path, db_pool):
    """A row whose watched_path does not exist is logged and skipped, not raised."""
    _seed_watcher("ghost", "/nonexistent/path/that/should/not/exist")
    await _seed_recent_run_with_path("ghost", "/tmp/never.py")

    # Must not raise.
    await watcher_tick(datetime.now(UTC))


# ---------------------------------------------------------------------------
# Heartbeat — _heartbeat upserts with watcher daemon_id
# ---------------------------------------------------------------------------


async def test_watcher_tick_writes_heartbeat_with_watcher_suffix(db_pool):
    await watcher_tick(datetime.now(UTC))

    hb = await fetch_one(
        "SELECT daemon_id, last_tick_at FROM daemon_heartbeats WHERE daemon_id = %s",
        (watcher_daemon_id(),),
    )
    assert hb is not None
    assert hb["daemon_id"].endswith(":watcher")


async def test_watcher_heartbeat_does_not_collide_with_scheduler_heartbeat(db_pool):
    """The watcher and scheduler use different daemon_id values."""
    # Seed a fake scheduler heartbeat (no ':watcher' suffix).
    import socket

    scheduler_id = socket.gethostname()
    await execute_query(
        "INSERT INTO daemon_heartbeats (daemon_id, started_at, last_tick_at) "
        "VALUES (%s, now(), now())",
        (scheduler_id,),
    )

    await watcher_tick(datetime.now(UTC))

    rows = await fetch_all("SELECT daemon_id FROM daemon_heartbeats ORDER BY daemon_id")
    daemon_ids = [r["daemon_id"] for r in rows]
    assert scheduler_id in daemon_ids
    assert watcher_daemon_id() in daemon_ids
    assert scheduler_id != watcher_daemon_id()


# ---------------------------------------------------------------------------
# Crash recovery — file-triggered orphans
# ---------------------------------------------------------------------------


async def test_recover_orphans_marks_file_triggered_running_rows_failed(db_pool):
    """A 'running' row with event='file' is marked failed on boot."""
    import json as _json

    await execute_query(
        "INSERT INTO workflow_runs "
        "(workflow_name, workflow_file_path, status, started_at, trigger_context) "
        "VALUES (%s, %s, 'running', now(), %s::jsonb)",
        ("crashed", "/tmp/c.py", _json.dumps({"event": "file", "path": "/x"})),
    )

    recovered = await _recover_orphans()
    assert recovered == 1

    row = await fetch_one(
        "SELECT status, error FROM workflow_runs WHERE workflow_name = %s",
        ("crashed",),
    )
    assert row["status"] == "failed"
    assert "watcher daemon restarted" in row["error"]


async def test_recover_orphans_does_not_touch_scheduler_running_rows(db_pool):
    """A 'running' row with no trigger_context (cron/manual) is untouched."""
    await execute_query(
        "INSERT INTO workflow_runs "
        "(workflow_name, workflow_file_path, status, started_at) "
        "VALUES (%s, %s, 'running', now())",
        ("cron_running", "/tmp/sched.py"),
    )
    # Also a webhook-triggered orphan — should not be recovered either.
    import json as _json

    await execute_query(
        "INSERT INTO workflow_runs "
        "(workflow_name, workflow_file_path, status, started_at, trigger_context) "
        "VALUES (%s, %s, 'running', now(), %s::jsonb)",
        ("webhook_running", "/tmp/wh.py", _json.dumps({"event": "webhook"})),
    )

    recovered = await _recover_orphans()
    assert recovered == 0

    cron_row = await fetch_one(
        "SELECT status FROM workflow_runs WHERE workflow_name = %s",
        ("cron_running",),
    )
    webhook_row = await fetch_one(
        "SELECT status FROM workflow_runs WHERE workflow_name = %s",
        ("webhook_running",),
    )
    assert cron_row["status"] == "running"
    assert webhook_row["status"] == "running"


# ---------------------------------------------------------------------------
# trigger_context populated correctly via the runner path
# ---------------------------------------------------------------------------


async def test_run_workflow_with_file_trigger_context_persists_to_row(db_pool):
    """The runner-side contract used by _fire_one populates trigger_context correctly."""
    result = await run_workflow(
        Workflow(name="ctx", steps=[ShellStep(name="s", command="echo hi")]),
        "ctx.py",
        trigger_context={
            "event": "file",
            "body": None,
            "headers": {},
            "path": "/data/in/note.md",
        },
    )
    row = await fetch_one(
        "SELECT trigger_context FROM workflow_runs WHERE id = %s",
        (result.id,),
    )
    assert row["trigger_context"]["event"] == "file"
    assert row["trigger_context"]["path"] == "/data/in/note.md"


# ---------------------------------------------------------------------------
# Observer pool sync — removed rows are stopped, path changes restart
# ---------------------------------------------------------------------------


async def test_observer_pool_stops_observers_for_disabled_rows(tmp_path, db_pool):
    """A row that gets disabled between ticks has its observer torn down."""
    from src.triggers.watcher import _pool

    watch_dir = tmp_path / "stopme"
    watch_dir.mkdir()
    _seed_watcher("stopme", str(watch_dir))

    await watcher_tick(datetime.now(UTC))
    assert len(_pool._live) == 1

    # Disable the row.
    await execute_query(
        "UPDATE file_watchers SET enabled = false WHERE workflow_name = %s",
        ("stopme",),
    )

    await watcher_tick(datetime.now(UTC))
    assert len(_pool._live) == 0


# ---------------------------------------------------------------------------
# brain watcher-status CLI command
# ---------------------------------------------------------------------------


def _seed_watcher_heartbeat(at: datetime) -> None:
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO daemon_heartbeats (daemon_id, started_at, last_tick_at) "
            "VALUES (%s, %s, %s)",
            (watcher_daemon_id(), at, at),
        )


def test_watcher_status_exits_1_when_no_heartbeat_row():
    """A clean DB has no heartbeat — watcher-status reports unhealthy."""
    from click.testing import CliRunner

    from src.cli import cli

    result = CliRunner().invoke(cli, ["watcher-status"])
    assert result.exit_code == 1
    assert "no heartbeat row" in result.output


def test_watcher_status_exits_0_when_heartbeat_is_recent():
    """A heartbeat within the 30-second threshold is healthy."""
    from datetime import timedelta

    from click.testing import CliRunner

    from src.cli import cli

    recent = datetime.now(UTC) - timedelta(seconds=5)
    _seed_watcher_heartbeat(recent)

    result = CliRunner().invoke(cli, ["watcher-status"])
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_watcher_status_exits_1_when_heartbeat_is_stale():
    """A heartbeat older than 30s means the watcher died or hung."""
    from datetime import timedelta

    from click.testing import CliRunner

    from src.cli import cli

    stale = datetime.now(UTC) - timedelta(seconds=120)
    _seed_watcher_heartbeat(stale)

    result = CliRunner().invoke(cli, ["watcher-status"])
    assert result.exit_code == 1
    assert "unhealthy" in result.output
