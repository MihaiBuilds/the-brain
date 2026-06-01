"""Tests for ``src.scheduler.daemon``.

These exercise ``daemon_tick(now)`` directly with a controlled clock
rather than spawning ``run_daemon``. The tick is the unit of behavior;
the polling loop is a thin wrapper over it.

Workflows here use ShellStep only so they are hermetic without mocking
HTTP. Schedule rows and workflow_runs rows are inspected by the same
synchronous psycopg pattern used in ``test_cli.py`` and
``test_lifecycle_cli.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from src.scheduler.daemon import (
    ORPHAN_ERROR,
    _recover_orphans,
    daemon_id,
    daemon_tick,
)

_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workflow(tmp_path, name, workflow_name=None, command="echo hi"):
    wf_name = workflow_name or name.removesuffix(".py")
    path = tmp_path / name
    path.write_text(
        "from src.workflow import Workflow, ShellStep\n"
        f"workflow = Workflow(name='{wf_name}', steps=[ShellStep(name='s', command='{command}')])\n"
    )
    return str(path)


def _seed_schedule(
    name,
    file_path,
    *,
    cron="*/5 * * * *",
    enabled=True,
    next_run_at,
    last_run_id=None,
):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO workflow_schedules
                (workflow_name, workflow_file_path, cron_expression,
                 enabled, next_run_at, last_run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (name, file_path, cron, enabled, next_run_at, last_run_id),
        )


def _seed_running_orphan(workflow_name="ghost"):
    """Insert one workflow_runs row with status='running' (simulates a crash)."""
    run_id = uuid4()
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs
                (id, workflow_name, workflow_file_path, started_at, status)
            VALUES (%s, %s, %s, %s, 'running')
            """,
            (run_id, workflow_name, f"{workflow_name}.py", datetime.now(UTC)),
        )
    return run_id


def _fetch_schedule(name):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.row_factory = dict_row
        return conn.execute(
            "SELECT * FROM workflow_schedules WHERE workflow_name = %s",
            (name,),
        ).fetchone()


def _fetch_run(run_id):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.row_factory = dict_row
        return conn.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()


def _fetch_heartbeat():
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.row_factory = dict_row
        return conn.execute("SELECT * FROM daemon_heartbeats").fetchone()


def _fetch_runs_for(workflow_name):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.row_factory = dict_row
        return conn.execute(
            "SELECT * FROM workflow_runs WHERE workflow_name = %s ORDER BY started_at",
            (workflow_name,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def test_tick_writes_a_heartbeat_row(db_pool):
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    await daemon_tick(now)

    heartbeat = _fetch_heartbeat()
    assert heartbeat is not None
    assert heartbeat["daemon_id"] == daemon_id()
    assert heartbeat["last_tick_at"] == now
    assert heartbeat["started_at"] == now


async def test_tick_updates_last_tick_at_but_preserves_started_at(db_pool):
    first = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    second = first + timedelta(seconds=10)

    await daemon_tick(first)
    await daemon_tick(second)

    heartbeat = _fetch_heartbeat()
    assert heartbeat["started_at"] == first
    assert heartbeat["last_tick_at"] == second


# ---------------------------------------------------------------------------
# Firing due workflows
# ---------------------------------------------------------------------------


async def test_tick_fires_a_due_workflow_and_advances_next_run_at(db_pool, tmp_path):
    file_path = _write_workflow(tmp_path, "due.py")
    past = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    now = past + timedelta(seconds=5)

    _seed_schedule("due", file_path, next_run_at=past)

    await daemon_tick(now)

    runs = _fetch_runs_for("due")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"

    schedule = _fetch_schedule("due")
    assert schedule["last_run_id"] == runs[0]["id"]
    # Next fire is computed from `now`, not from the original next_run_at.
    assert schedule["next_run_at"] > now


async def test_tick_does_not_fire_a_workflow_whose_next_run_at_is_in_the_future(db_pool, tmp_path):
    file_path = _write_workflow(tmp_path, "later.py")
    future = datetime(2026, 6, 1, 12, 5, 0, tzinfo=UTC)
    now = future - timedelta(seconds=10)

    _seed_schedule("later", file_path, next_run_at=future)

    await daemon_tick(now)

    assert _fetch_runs_for("later") == []
    # next_run_at is untouched.
    assert _fetch_schedule("later")["next_run_at"] == future


async def test_tick_skips_disabled_schedules(db_pool, tmp_path):
    file_path = _write_workflow(tmp_path, "off.py")
    past = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    now = past + timedelta(minutes=5)

    _seed_schedule("off", file_path, enabled=False, next_run_at=past)

    await daemon_tick(now)

    assert _fetch_runs_for("off") == []
    # next_run_at is NOT advanced for disabled schedules — they're invisible to the daemon.
    assert _fetch_schedule("off")["next_run_at"] == past


async def test_skip_not_catchup_anchors_next_fire_on_now_not_on_original_slot(db_pool, tmp_path):
    """A schedule that fell hours behind fires exactly once, then advances past `now`."""
    file_path = _write_workflow(tmp_path, "lag.py")
    long_ago = datetime(2026, 6, 1, 6, 0, 0, tzinfo=UTC)
    now = long_ago + timedelta(hours=6)  # six hours overdue

    _seed_schedule("lag", file_path, cron="*/5 * * * *", next_run_at=long_ago)

    await daemon_tick(now)

    runs = _fetch_runs_for("lag")
    assert len(runs) == 1  # Exactly one fire, not 72 catch-up fires.

    schedule = _fetch_schedule("lag")
    # Next fire is the next cron boundary AFTER now, not after long_ago.
    assert schedule["next_run_at"] > now
    assert schedule["next_run_at"] < now + timedelta(minutes=6)


async def test_multiple_due_workflows_fire_in_most_overdue_first_order(db_pool, tmp_path):
    early_file = _write_workflow(tmp_path, "early.py")
    late_file = _write_workflow(tmp_path, "late.py")

    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    now = base + timedelta(minutes=5)

    _seed_schedule("late", late_file, next_run_at=base + timedelta(minutes=1))
    _seed_schedule("early", early_file, next_run_at=base)

    await daemon_tick(now)

    early_run = _fetch_runs_for("early")[0]
    late_run = _fetch_runs_for("late")[0]
    # Most-overdue (smaller next_run_at) fires first.
    assert early_run["started_at"] <= late_run["started_at"]


# ---------------------------------------------------------------------------
# Missing / broken workflow file at fire time
# ---------------------------------------------------------------------------


async def test_tick_skips_missing_workflow_file_but_advances_next_run_at(db_pool, tmp_path):
    """The schedule stays enabled; the daemon logs and moves on."""
    missing = str(tmp_path / "ghost.py")
    past = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    now = past + timedelta(minutes=1)

    _seed_schedule("ghost", missing, next_run_at=past)

    await daemon_tick(now)

    # No workflow_runs row — there was nothing to run.
    assert _fetch_runs_for("ghost") == []

    schedule = _fetch_schedule("ghost")
    assert schedule["enabled"] is True  # Still enabled — user fixes the file.
    assert schedule["next_run_at"] > now  # Advanced — no log spam every 10s.
    assert schedule["last_run_id"] is None


# ---------------------------------------------------------------------------
# Schedule list refresh — new registrations land on the next tick
# ---------------------------------------------------------------------------


async def test_tick_picks_up_newly_registered_schedules(db_pool, tmp_path):
    """The tick re-reads workflow_schedules every call — no caching."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

    # First tick: nothing registered.
    await daemon_tick(now)
    assert _fetch_runs_for("fresh") == []

    # Now register, with next_run_at in the past so it's immediately due.
    file_path = _write_workflow(tmp_path, "fresh.py")
    _seed_schedule("fresh", file_path, next_run_at=now - timedelta(seconds=1))

    await daemon_tick(now)
    assert len(_fetch_runs_for("fresh")) == 1


# ---------------------------------------------------------------------------
# Crash recovery — orphan runs
# ---------------------------------------------------------------------------


async def test_recover_orphans_marks_running_rows_as_failed(db_pool):
    run_id = _seed_running_orphan("ghost-run")

    recovered = await _recover_orphans()
    assert recovered == 1

    row = _fetch_run(run_id)
    assert row["status"] == "failed"
    assert row["error"] == ORPHAN_ERROR
    assert row["ended_at"] is not None


async def test_recover_orphans_is_a_noop_when_no_running_rows_exist(db_pool):
    recovered = await _recover_orphans()
    assert recovered == 0


def _seed_heartbeat(daemon_id_value, last_tick_at, started_at=None):
    """Insert a heartbeat row directly. Used by daemon-status tests."""
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO daemon_heartbeats (daemon_id, started_at, last_tick_at)
            VALUES (%s, %s, %s)
            """,
            (daemon_id_value, started_at or last_tick_at, last_tick_at),
        )


# ---------------------------------------------------------------------------
# brain daemon-status — healthcheck command
# ---------------------------------------------------------------------------


def test_daemon_status_exits_one_when_no_heartbeat_row_exists():
    """A daemon that has never started has no row to read."""
    from click.testing import CliRunner

    from src.cli import cli

    result = CliRunner().invoke(cli, ["daemon-status"])
    assert result.exit_code == 1
    assert "no heartbeat row" in result.output


def test_daemon_status_exits_zero_when_heartbeat_is_recent():
    """A heartbeat within the 30-second threshold is healthy."""
    from click.testing import CliRunner

    from src.cli import cli

    recent = datetime.now(UTC) - timedelta(seconds=5)
    _seed_heartbeat("test-daemon", recent)

    result = CliRunner().invoke(cli, ["daemon-status"])
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output
    assert "test-daemon" in result.output


def test_daemon_status_exits_one_when_heartbeat_is_stale():
    """A heartbeat older than 30s means the daemon died or hung."""
    from click.testing import CliRunner

    from src.cli import cli

    stale = datetime.now(UTC) - timedelta(seconds=120)
    _seed_heartbeat("test-daemon", stale)

    result = CliRunner().invoke(cli, ["daemon-status"])
    assert result.exit_code == 1
    assert "unhealthy" in result.output
    assert "120s ago" in result.output or "121s ago" in result.output


def test_daemon_status_uses_most_recent_heartbeat_if_multiple_rows_exist():
    """If a daemon_id ever changes (e.g. container hostname swap), the latest tick wins."""
    from click.testing import CliRunner

    from src.cli import cli

    stale = datetime.now(UTC) - timedelta(seconds=120)
    recent = datetime.now(UTC) - timedelta(seconds=5)
    _seed_heartbeat("old-daemon", stale)
    _seed_heartbeat("new-daemon", recent)

    result = CliRunner().invoke(cli, ["daemon-status"])
    assert result.exit_code == 0, result.output
    assert "new-daemon" in result.output


async def test_recover_orphans_does_not_touch_terminal_rows(db_pool, tmp_path):
    """Already-failed or already-successful rows are untouched."""
    file_path = _write_workflow(tmp_path, "ok.py")
    past = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    _seed_schedule("ok", file_path, next_run_at=past)
    await daemon_tick(past + timedelta(seconds=1))  # Drives a real run to success.

    orphan_id = _seed_running_orphan("ghost")

    recovered = await _recover_orphans()

    # The successful row from the real run is untouched.
    success_run = _fetch_runs_for("ok")[0]
    assert success_run["status"] == "success"

    # Only the orphan was recovered.
    assert recovered == 1
    assert _fetch_run(orphan_id)["status"] == "failed"
