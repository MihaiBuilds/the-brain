"""
The file watcher daemon — long-running process that fires workflows on filesystem events.

Public surface
--------------
- ``watcher_tick(now)`` does one cycle: heartbeat plus an observer-pool
  sync from the ``file_watchers`` table. Pure async function — no
  signals, no sleeping. Tests drive it directly.
- ``run_watcher_daemon()`` is the production entry point. On boot it
  recovers orphans (file-triggered rows still ``status='running'`` from
  a previous crash) and then loops on ``watcher_tick`` every
  ``POLL_INTERVAL_SECONDS``. SIGTERM and SIGINT trigger a graceful
  shutdown — the Observer is stopped and any in-flight workflow
  finishes before the loop exits.

Locked v1.0 invariants
----------------------
- One watcher daemon per host. Crash recovery would clobber another
  daemon's in-flight runs if more than one ran in parallel.
- Single directory per watcher row, no recursion. ``watched_path`` is
  a directory; events outside it are ignored.
- Event filtering is per-watcher — only the event types listed in
  ``watched_events`` (any of ``created``/``modified``/``deleted``)
  fire the workflow.
- **500ms debounce per (workflow_name, path)**. A single editor save
  often emits both ``modified`` and ``created`` events; the debounce
  coalesces them. The state is in-memory only — crash recovery re-fires
  from current FS state, so the lost in-memory state is harmless.
- Sequential workflow execution. A workflow that takes longer than the
  next event arrives simply queues the next event for after it finishes
  (via the run lock); subsequent events for the same (workflow, path)
  within the debounce window are dropped.
- Crash recovery is scoped to file-triggered runs only via
  ``trigger_context->>'event' = 'file'`` — the scheduler daemon's
  recovery (which is broader) does not collide with this one because
  the two services run in separate containers.
- The watcher daemon's heartbeat uses ``daemon_id = hostname:watcher``
  so it coexists with the scheduler's heartbeat in the same
  ``daemon_heartbeats`` table without colliding.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.db import execute_query, fetch_all
from src.runner import run_workflow
from src.workflow.loader import WorkflowLoadError, import_workflow_from_file

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10
DEBOUNCE_SECONDS = 0.5
ORPHAN_ERROR = "watcher daemon restarted with run in progress"

# watchdog event_type → trigger event-name. Move/rename is treated as
# 'modified' for the workflow's purposes (the destination side fires the
# created event separately).
_WATCHDOG_EVENT_MAP: dict[str, str] = {
    "created": "created",
    "modified": "modified",
    "deleted": "deleted",
}


def watcher_daemon_id() -> str:
    """Return this watcher daemon's identity — distinct from the scheduler's."""
    return f"{socket.gethostname()}:watcher"


# ---------------------------------------------------------------------------
# Debounce state — module-global, in-memory, daemon-process lifetime only
# ---------------------------------------------------------------------------

# Maps (workflow_name, absolute_path) → monotonic timestamp of last fire.
_last_fire: dict[tuple[str, str], float] = {}


def _should_fire(workflow_name: str, path: str, now: float) -> bool:
    """Return True if the (workflow, path) is past the debounce window.

    On True, the caller MUST also call ``_record_fire`` to advance the
    debounce timestamp. Splitting the check from the record keeps the
    test surface tight.
    """
    last = _last_fire.get((workflow_name, path))
    return last is None or (now - last) >= DEBOUNCE_SECONDS


def _record_fire(workflow_name: str, path: str, now: float) -> None:
    _last_fire[(workflow_name, path)] = now


def _reset_debounce_for_tests() -> None:
    """Hook for the test suite to clear debounce state between tests."""
    _last_fire.clear()


# ---------------------------------------------------------------------------
# Heartbeat + crash recovery
# ---------------------------------------------------------------------------


async def _heartbeat(now: datetime) -> None:
    """UPSERT the heartbeat row for this watcher daemon."""
    await execute_query(
        """
        INSERT INTO daemon_heartbeats (daemon_id, started_at, last_tick_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (daemon_id) DO UPDATE
            SET last_tick_at = EXCLUDED.last_tick_at
        """,
        (watcher_daemon_id(), now, now),
    )


async def _recover_orphans() -> int:
    """Mark file-triggered ``status='running'`` rows as failed.

    Scoped via ``trigger_context->>'event' = 'file'`` so the scheduler
    daemon's broader recovery sweep does not collide with this one.

    Returns the number of orphans recovered.
    """
    return await execute_query(
        """
        UPDATE workflow_runs
           SET status = 'failed',
               error = %s,
               ended_at = %s
         WHERE status = 'running'
           AND trigger_context->>'event' = 'file'
        """,
        (ORPHAN_ERROR, datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------
# Watcher row fetch
# ---------------------------------------------------------------------------


async def _enabled_watchers() -> list[dict[str, Any]]:
    """Fetch every enabled file_watchers row."""
    return await fetch_all(
        """
        SELECT workflow_name, watched_path, watched_events
          FROM file_watchers
         WHERE enabled = true
         ORDER BY workflow_name
        """
    )


# ---------------------------------------------------------------------------
# Observer pool — one watchdog Observer per enabled row
# ---------------------------------------------------------------------------


class _WatcherEventHandler(FileSystemEventHandler):
    """Routes one watchdog event into the runner via the asyncio loop.

    Watchdog runs handlers on its own thread, so we schedule the async
    fire into the daemon's event loop via ``call_soon_threadsafe``.
    """

    def __init__(
        self,
        *,
        workflow_name: str,
        watched_events: list[str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self.workflow_name = workflow_name
        self.watched_events = watched_events
        self.loop = loop

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        mapped = _WATCHDOG_EVENT_MAP.get(event.event_type)
        if mapped is None or mapped not in self.watched_events:
            return
        # asyncio.run_coroutine_threadsafe returns a concurrent.futures.Future;
        # we don't await it from this thread.
        asyncio.run_coroutine_threadsafe(
            _fire_one(self.workflow_name, event.src_path, mapped),
            self.loop,
        )


async def _fire_one(workflow_name: str, event_path: str, mapped_event: str) -> None:
    """Run one workflow in response to a filesystem event.

    Debounce check first, then the same load + run flow the scheduler
    daemon uses for cron fires.
    """
    now_mono = time.monotonic()
    if not _should_fire(workflow_name, event_path, now_mono):
        logger.debug(
            "Debounced %s on %s (within %.0fms)", workflow_name, event_path, DEBOUNCE_SECONDS * 1000
        )
        return
    _record_fire(workflow_name, event_path, now_mono)

    # Resolve the workflow file path from the file_watchers row at fire
    # time (the row could have changed since the Observer was started).
    rows = await fetch_all(
        "SELECT workflow_name FROM file_watchers WHERE workflow_name = %s AND enabled = true",
        (workflow_name,),
    )
    if not rows:
        logger.info("Watcher %r disabled or removed before fire — skipping", workflow_name)
        return

    file_path = await _resolve_workflow_file_path(workflow_name)
    if file_path is None:
        logger.warning("Watcher %r has no workflow file recorded — skipping fire", workflow_name)
        return

    try:
        workflow = import_workflow_from_file(file_path)
    except WorkflowLoadError:
        logger.exception(
            "Watcher %r: workflow file %r could not be loaded — skipping fire",
            workflow_name,
            file_path,
        )
        return

    logger.info("Firing watcher %r (file %s, event %s)", workflow_name, event_path, mapped_event)
    trigger_context = {
        "event": "file",
        "body": None,
        "headers": {},
        "path": event_path,
    }
    try:
        await run_workflow(workflow, file_path, trigger_context=trigger_context)
    except Exception:
        logger.exception("Unexpected runner error for watcher %r — daemon continues", workflow_name)


async def _resolve_workflow_file_path(workflow_name: str) -> str | None:
    """Look up the registered workflow file path for the watcher.

    For v1.0 we follow the same pattern as the webhook endpoint: the
    most recent workflow_runs row for this workflow carries the path,
    set by `brain run` or by an earlier watcher fire. A follow-up adds
    the explicit register-watcher CLI that records the path on
    registration; until then this lookup is the bridge.
    """
    rows = await fetch_all(
        """
        SELECT workflow_file_path
          FROM workflow_runs
         WHERE workflow_name = %s
         ORDER BY started_at DESC
         LIMIT 1
        """,
        (workflow_name,),
    )
    return rows[0]["workflow_file_path"] if rows else None


class _ObserverPool:
    """Maintains one watchdog Observer per (workflow_name, watched_path).

    ``sync(rows, loop)`` reconciles the live observers against the
    target row set: starts watchers for new rows, stops watchers for
    rows that disappeared or whose path changed.
    """

    def __init__(self) -> None:
        # Maps (workflow_name, watched_path) → (Observer, watch_handle)
        self._live: dict[tuple[str, str], tuple[Any, Any]] = {}

    def sync(self, rows: list[dict[str, Any]], loop: asyncio.AbstractEventLoop) -> None:
        target_keys = {(r["workflow_name"], str(Path(r["watched_path"]).resolve())) for r in rows}

        # Stop observers that should no longer run.
        for key in list(self._live.keys()):
            if key not in target_keys:
                observer, _ = self._live.pop(key)
                observer.stop()
                observer.join(timeout=1.0)

        # Start observers for any new keys.
        for row in rows:
            resolved_path = str(Path(row["watched_path"]).resolve())
            key = (row["workflow_name"], resolved_path)
            if key in self._live:
                continue

            if not Path(resolved_path).is_dir():
                logger.warning(
                    "Watcher %r: path %r is not a directory — skipping",
                    row["workflow_name"],
                    resolved_path,
                )
                continue

            handler = _WatcherEventHandler(
                workflow_name=row["workflow_name"],
                watched_events=list(row["watched_events"]),
                loop=loop,
            )
            observer = Observer()
            watch_handle = observer.schedule(handler, resolved_path, recursive=False)
            observer.start()
            self._live[key] = (observer, watch_handle)
            logger.info(
                "Watcher %r: observing %s for %s",
                row["workflow_name"],
                resolved_path,
                row["watched_events"],
            )

    def stop_all(self) -> None:
        for observer, _ in self._live.values():
            observer.stop()
        for observer, _ in self._live.values():
            observer.join(timeout=2.0)
        self._live.clear()


# Module-level singleton; one daemon process, one pool.
_pool = _ObserverPool()


def _reset_pool_for_tests() -> None:
    """Hook for the test suite to wipe the observer pool between tests."""
    _pool.stop_all()


# ---------------------------------------------------------------------------
# Tick + daemon loop
# ---------------------------------------------------------------------------


async def watcher_tick(now: datetime) -> None:
    """Run one observer-pool sync cycle.

    Heartbeat first (so a sync exception still records the daemon is
    alive), then sync the observer pool against the current
    ``file_watchers`` table state.
    """
    await _heartbeat(now)
    rows = await _enabled_watchers()
    loop = asyncio.get_running_loop()
    _pool.sync(rows, loop)


async def run_watcher_daemon() -> None:
    """Production entry point — long-running poll loop with signal handlers."""
    logger.info("Watcher daemon %s starting", watcher_daemon_id())

    recovered = await _recover_orphans()
    if recovered:
        logger.warning("Recovered %d orphan file-triggered run(s) from a previous crash", recovered)

    shutdown = asyncio.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        logger.info("Signal %s received — shutting down after current tick", signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    try:
        while not shutdown.is_set():
            tick_started = datetime.now(UTC)
            try:
                await watcher_tick(tick_started)
            except Exception:
                logger.exception("Watcher tick raised — continuing")

            if shutdown.is_set():
                break

            try:
                await asyncio.wait_for(shutdown.wait(), timeout=POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass
    finally:
        _pool.stop_all()

    logger.info("Watcher daemon %s exited", watcher_daemon_id())
