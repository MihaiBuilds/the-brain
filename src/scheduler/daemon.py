"""
The scheduler daemon — long-running process that fires due workflows.

Public surface
--------------
- ``daemon_tick(now)`` does one poll cycle: heartbeat, find due workflows,
  fire each sequentially, advance ``next_run_at``. Pure async function;
  no signals, no sleeping. Tests drive it manually with a frozen clock.
- ``run_daemon()`` is the production entry point. On boot it recovers
  orphans (rows still ``status='running'`` from a previous crash) and
  then loops on ``daemon_tick`` every ``POLL_INTERVAL_SECONDS``. SIGTERM
  and SIGINT trigger a graceful shutdown — the in-flight workflow
  finishes before the loop exits.

Locked v1.0 invariants
----------------------
- One daemon per host. Crash recovery would clobber another daemon's
  in-flight runs if more than one ran in parallel.
- Sequential workflow execution within a tick — no concurrency, no
  queue. A long-running workflow simply delays the next tick.
- Skip-not-catch-up: a workflow whose ``next_run_at`` slid into the
  past fires exactly once, then advances to ``cron.next_fire_after(now)``.
- 10-second polling. Cron precision is minute-level, so 10s is enough
  resolution; sleep-until-next-due is over-engineering for v1.0.
- A workflow whose registered file is missing or unloadable at fire
  time logs an error, advances ``next_run_at`` past the current moment,
  and leaves the schedule enabled. The user fixes the file.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from datetime import UTC, datetime
from typing import Any

from src.db import execute_query, fetch_all
from src.runner import run_workflow
from src.scheduler.cron import CronExpression, InvalidCronError
from src.workflow.loader import WorkflowLoadError, import_workflow_from_file

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10
ORPHAN_ERROR = "daemon restarted with run in progress"


def daemon_id() -> str:
    """Return this daemon's identity — the container/host name."""
    return socket.gethostname()


async def _heartbeat(now: datetime) -> None:
    """UPSERT the heartbeat row for this daemon.

    ``started_at`` is preserved across ticks; only ``last_tick_at`` advances.
    """
    await execute_query(
        """
        INSERT INTO daemon_heartbeats (daemon_id, started_at, last_tick_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (daemon_id) DO UPDATE
            SET last_tick_at = EXCLUDED.last_tick_at
        """,
        (daemon_id(), now, now),
    )


async def _due_schedules(now: datetime) -> list[dict[str, Any]]:
    """Fetch enabled schedules whose ``next_run_at`` is at or before ``now``.

    Most-overdue first so a backlog drains in the order it built up.
    """
    return await fetch_all(
        """
        SELECT workflow_name, workflow_file_path, cron_expression
          FROM workflow_schedules
         WHERE enabled = true AND next_run_at <= %s
         ORDER BY next_run_at ASC
        """,
        (now,),
    )


async def _advance_next_fire(workflow_name: str, cron_expr: str, now: datetime) -> None:
    """Recompute ``next_run_at`` after a fire and write it back.

    Skip-not-catch-up: the new fire time is anchored on ``now``, never on
    the prior ``next_run_at``. A workflow that fell hours behind catches
    up to the next scheduled fire after right-now, not to the next fire
    after the slot it missed.
    """
    try:
        cron = CronExpression.parse(cron_expr)
    except InvalidCronError:
        # A schedule row with a corrupt cron expression should never exist
        # (register validates before INSERT) but guard the daemon anyway.
        logger.exception("Schedule %r has invalid cron %r — skipping", workflow_name, cron_expr)
        return

    next_fire = cron.next_fire_after(now)
    await execute_query(
        "UPDATE workflow_schedules SET next_run_at = %s WHERE workflow_name = %s",
        (next_fire, workflow_name),
    )


async def _fire_one(schedule: dict[str, Any], now: datetime) -> None:
    """Run one due workflow and advance its ``next_run_at``.

    A missing or unloadable workflow file logs an error, advances
    ``next_run_at``, and returns — the schedule stays enabled. No
    ``workflow_runs`` row is written when the file can't be loaded;
    there is nothing to run.
    """
    name = schedule["workflow_name"]
    file_path = schedule["workflow_file_path"]

    try:
        workflow = import_workflow_from_file(file_path)
    except WorkflowLoadError:
        logger.exception(
            "Schedule %r: workflow file %r could not be loaded — skipping fire", name, file_path
        )
        await _advance_next_fire(name, schedule["cron_expression"], now)
        return

    logger.info("Firing schedule %r (file %s)", name, file_path)
    workflow_run = await run_workflow(workflow, file_path)

    # Link the run back to the schedule for `brain list` to surface,
    # then advance next_run_at to the next cron boundary after right-now.
    await execute_query(
        "UPDATE workflow_schedules SET last_run_id = %s WHERE workflow_name = %s",
        (workflow_run.id, name),
    )
    await _advance_next_fire(name, schedule["cron_expression"], now)


async def daemon_tick(now: datetime) -> None:
    """Run one poll cycle.

    Heartbeat first (so an exception in firing still records that the
    daemon is alive), then drain every workflow that is due at ``now``,
    sequentially. Each workflow's failure is its own concern — the
    runner persists a failed row, the daemon logs and moves on.
    """
    await _heartbeat(now)

    schedules = await _due_schedules(now)
    if not schedules:
        return

    logger.info("%d schedule(s) due at %s", len(schedules), now.isoformat())
    for schedule in schedules:
        try:
            await _fire_one(schedule, now)
        except Exception:
            # The runner already persisted a failed row on its own
            # failures; this catch is for anything outside the runner
            # (DB hiccup, unexpected). Keep the daemon alive.
            logger.exception(
                "Unexpected error firing %r — daemon continues", schedule["workflow_name"]
            )


async def _recover_orphans() -> int:
    """Mark any ``status='running'`` row as failed.

    Called once on daemon boot. Under the single-daemon-per-host
    invariant, any ``running`` row at startup is an orphan from a prior
    crash — the prior daemon's process died mid-run and the runner
    never got to write the terminal row.

    Returns the number of orphans recovered.
    """
    return await execute_query(
        """
        UPDATE workflow_runs
           SET status = 'failed',
               error = %s,
               ended_at = %s
         WHERE status = 'running'
        """,
        (ORPHAN_ERROR, datetime.now(UTC)),
    )


async def run_daemon() -> None:
    """Production entry point — long-running poll loop with signal handlers."""
    logger.info("Daemon %s starting", daemon_id())

    recovered = await _recover_orphans()
    if recovered:
        logger.warning("Recovered %d orphan run(s) from a previous crash", recovered)

    shutdown = asyncio.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        logger.info("Signal %s received — shutting down after current tick", signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    while not shutdown.is_set():
        tick_started = datetime.now(UTC)
        try:
            await daemon_tick(tick_started)
        except Exception:
            logger.exception("Daemon tick raised — continuing")

        if shutdown.is_set():
            break

        try:
            # Sleep on the shutdown event so SIGTERM wakes us immediately
            # instead of waiting out the full 10 seconds.
            await asyncio.wait_for(shutdown.wait(), timeout=POLL_INTERVAL_SECONDS)
        except TimeoutError:
            pass

    logger.info("Daemon %s exited", daemon_id())
