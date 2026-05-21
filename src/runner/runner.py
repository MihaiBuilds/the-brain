"""Executes a workflow step by step and persists the run to Postgres.

``run_workflow`` is the single entry point. It records a ``running`` row
before the first step, runs steps in order, halts on the first failure,
and always finishes by writing a terminal row — ``success`` or ``failed``.

Placeholder substitution
------------------------
Before a step runs, ``{prior_step_name}`` tokens in its string fields are
replaced with that prior step's output. Substitution happens here, in the
runner; executors receive an already-resolved step. An unknown placeholder
fails that step rather than leaking literal braces downstream.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from src.db import execute_query
from src.executors.base import StepResult, get_executor
from src.runner.models import WorkflowRun
from src.workflow.models import Step, Workflow

logger = logging.getLogger(__name__)

# Fields that may carry {placeholder} tokens, per step type.
_SUBSTITUTABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "memory_vault": ("query",),
    "llm": ("prompt", "system"),
    "shell": ("command",),
}

# A {name} token. Names follow the same shape as step names (no braces).
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class PlaceholderError(Exception):
    """A step referenced a {placeholder} with no matching prior step."""


def _substitute(text: str, results: dict[str, StepResult]) -> str:
    """Replace every {name} in ``text`` with that prior step's output.

    Raises:
        PlaceholderError: a token names a step that has not run.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in results:
            raise PlaceholderError(f"unknown placeholder {{{name}}} — no prior step named {name!r}")
        return results[name].output

    return _PLACEHOLDER.sub(replace, text)


def _resolve_step(step: Step, results: dict[str, StepResult]) -> Step:
    """Return a copy of ``step`` with its string fields substituted."""
    fields = _SUBSTITUTABLE_FIELDS.get(step.type, ())
    updates: dict[str, str] = {}
    for field in fields:
        value = getattr(step, field, None)
        if isinstance(value, str):
            updates[field] = _substitute(value, results)
    return step.model_copy(update=updates) if updates else step


async def run_workflow(
    workflow: Workflow,
    file_path: str,
    on_step_complete: Callable[[StepResult], None] | None = None,
) -> WorkflowRun:
    """Run a workflow end to end and persist the result.

    Inserts a ``running`` row, executes steps in order, and halts on the
    first failed step (later steps are skipped). The run always ends with
    a terminal row in ``workflow_runs`` — even if an executor raises.

    Args:
        workflow: the validated workflow to run.
        file_path: the path the workflow was loaded from, stored on the row.
        on_step_complete: optional callback invoked with each step's
            ``StepResult`` as it finishes — used for live progress output.
            Library callers can omit it; the runner stays unaware of any
            terminal or transport.

    Returns:
        The persisted WorkflowRun with its terminal status and output.
    """
    run_id = uuid4()
    started_at = datetime.now(UTC)

    await execute_query(
        """
        INSERT INTO workflow_runs
            (id, workflow_name, workflow_file_path, started_at, status)
        VALUES (%s, %s, %s, %s, 'running')
        """,
        (run_id, workflow.name, file_path, started_at),
    )
    logger.info("Run %s started — workflow %r", run_id, workflow.name)

    results: dict[str, StepResult] = {}
    status = "success"
    error: str | None = None

    for step in workflow.steps:
        try:
            resolved = _resolve_step(step, results)
        except PlaceholderError as e:
            result = StepResult(step_name=step.name, success=False, error=str(e))
        else:
            try:
                result = await get_executor(resolved).execute(resolved)
            except Exception as e:  # an executor should not raise — be safe.
                logger.exception("Step %r raised unexpectedly", step.name)
                result = StepResult(
                    step_name=step.name,
                    success=False,
                    error=f"executor raised unexpectedly: {e}",
                )

        results[step.name] = result
        if on_step_complete is not None:
            on_step_complete(result)
        if not result.success:
            status = "failed"
            error = f"step {step.name!r} failed: {result.error}"
            logger.warning("Run %s halted — %s", run_id, error)
            break

    ended_at = datetime.now(UTC)
    output = {name: {"success": r.success, "output": r.output} for name, r in results.items()}

    await execute_query(
        """
        UPDATE workflow_runs
        SET ended_at = %s, status = %s, output = %s, error = %s
        WHERE id = %s
        """,
        (ended_at, status, json.dumps(output), error, run_id),
    )
    logger.info("Run %s ended — %s", run_id, status)

    return WorkflowRun(
        id=run_id,
        workflow_name=workflow.name,
        workflow_file_path=file_path,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        output=output,
        error=error,
    )
