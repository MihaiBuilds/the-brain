"""Structured logging setup for The Brain.

Wires ``structlog`` to the stdlib ``logging`` module so every existing
``logger = logging.getLogger(__name__)`` call across ``src/`` gets a
structured output format without per-call rewrites.

Two renderers are available, chosen by the ``LOG_FORMAT`` env var:

- ``LOG_FORMAT=keyvalue`` (default) — human-readable ``key=value`` output
  for interactive CLI use. Easy to read in a terminal.
- ``LOG_FORMAT=json`` — newline-delimited JSON for production daemons
  (``brain daemon``, ``brain watcher``, ``brain serve``) running under
  Docker. Each log line is one JSON object suitable for ingestion by
  log aggregators or for diff-friendly inspection.

The Docker compose configuration sets ``LOG_FORMAT=json`` for the brain
services so production logs are structured by default; local CLI use
stays human-readable.

A ``bind_run_id`` context manager makes the workflow-run identifier
available as a structured field on every log line emitted inside its
scope, via ``structlog``'s ``contextvars`` binding. Existing log
formatting strings that already include the run ID (e.g.
``"Run %s started"``) keep working — the extra structured field is
redundant but harmless and lets log consumers filter on the field.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

_LOG_FORMAT_ENV = "LOG_FORMAT"
_DEFAULT_LOG_FORMAT = "keyvalue"
_LOG_LEVEL_ENV = "LOG_LEVEL"
_DEFAULT_LOG_LEVEL = "INFO"


def _resolve_renderer() -> structlog.types.Processor:
    """Pick the renderer based on the LOG_FORMAT env var."""
    fmt = os.environ.get(_LOG_FORMAT_ENV, _DEFAULT_LOG_FORMAT).lower()
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    # Default: human-readable key=value output for terminals.
    return structlog.dev.ConsoleRenderer(colors=False)


def configure_logging() -> None:
    """Initialise structlog + stdlib logging integration.

    Safe to call multiple times; each call rebinds the configuration.
    Intended to be called once from each entry point that owns its own
    process lifecycle (the CLI in ``src/cli.py``, and indirectly the
    daemons + HTTP API via the CLI commands that launch them).
    """
    level = os.environ.get(_LOG_LEVEL_ENV, _DEFAULT_LOG_LEVEL).upper()
    renderer = _resolve_renderer()

    # Shared processor chain — runs for events from structlog AND from
    # stdlib loggers routed through the ``ProcessorFormatter`` below.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to the stdlib formatter so structlog calls and
            # stdlib calls (from existing ``logger.info(...)`` sites)
            # both flow through one renderer.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Caching tied loggers to whatever configuration was in effect
        # the first time they were used — breaks test isolation when a
        # test changes LOG_FORMAT mid-run. The perf cost is negligible
        # for CLI workloads.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any existing handlers so re-calling configure_logging() in
    # tests or after a basicConfig() call from older code paths starts
    # cleanly rather than emitting each line twice.
    root.handlers = [handler]
    root.setLevel(level)


@contextmanager
def bind_run_id(run_id: object) -> Iterator[None]:
    """Bind ``run_id`` as a structured field for the duration of the block.

    Every log emitted inside the ``with`` block — from any module, via
    either structlog or stdlib ``logger`` calls routed through the
    shared processor chain — will carry a ``run_id`` key whose value is
    the stringified ``run_id`` argument. Used by the runner so a single
    workflow run's log lines can be filtered as a group from a busy
    multi-run log stream.
    """
    with structlog.contextvars.bound_contextvars(run_id=str(run_id)):
        yield
