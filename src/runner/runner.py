"""Executes a workflow step by step and persists the run to Postgres.

``run_workflow`` is the single entry point. It records a ``running`` row
before the first step, runs steps in order, halts on the first failure,
and always finishes by writing a terminal row — ``success`` or ``failed``.

Placeholder substitution
------------------------
Two token shapes are supported in string fields, both resolved here:

- ``{prior_step_name}`` — output of an earlier step in the SAME run.
- ``{previous.step_name}`` — output of the step with that name in the
  last SUCCESSFUL run of the same workflow.

Executors receive an already-resolved step. An unknown placeholder fails
THAT step with a clear error rather than leaking literal braces
downstream. ``{previous.X}`` with no prior successful run also fails the
step — strict by design, matching the M1 placeholder voice.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.db import execute_query, fetch_one
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


_PREVIOUS_PREFIX = "previous."


class PlaceholderError(Exception):
    """A step referenced a {placeholder} with no matching source."""


def _substitute(
    text: str,
    results: dict[str, StepResult],
    previous_steps: dict[str, str] | None,
) -> str:
    """Replace every ``{name}`` in ``text`` with the resolved value.

    A token starting with ``previous.`` looks up the step name from
    ``previous_steps`` (a name → output mapping for the last successful
    run). Any other token looks up a prior step's output in the current
    run via ``results``.

    Raises:
        PlaceholderError: a token resolves to no source — prior step
            missing, no previous successful run, or step missing from
            the previous run.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith(_PREVIOUS_PREFIX):
            step_name = name[len(_PREVIOUS_PREFIX) :]
            if previous_steps is None:
                raise PlaceholderError(
                    f"unknown placeholder {{{name}}} — no previous successful run of this workflow"
                )
            if step_name not in previous_steps:
                raise PlaceholderError(
                    f"unknown placeholder {{{name}}} — previous run has no step named {step_name!r}"
                )
            return previous_steps[step_name]
        if name not in results:
            raise PlaceholderError(f"unknown placeholder {{{name}}} — no prior step named {name!r}")
        return results[name].output

    return _PLACEHOLDER.sub(replace, text)


def _resolve_step(
    step: Step,
    results: dict[str, StepResult],
    previous_steps: dict[str, str] | None,
) -> Step:
    """Return a copy of ``step`` with its string fields substituted."""
    fields = _SUBSTITUTABLE_FIELDS.get(step.type, ())
    updates: dict[str, str] = {}
    for field in fields:
        value = getattr(step, field, None)
        if isinstance(value, str):
            updates[field] = _substitute(value, results, previous_steps)
    return step.model_copy(update=updates) if updates else step


async def _lookup_previous_run(workflow_name: str) -> tuple[UUID | None, dict[str, str] | None]:
    """Find the most recent successful run of ``workflow_name``.

    Returns ``(run_id, {step_name: output})`` if such a run exists,
    otherwise ``(None, None)``. The partial index on
    ``(workflow_name, started_at DESC) WHERE status = 'success'`` makes
    this an index-only lookup.
    """
    row = await fetch_one(
        """
        SELECT id, output
          FROM workflow_runs
         WHERE workflow_name = %s AND status = 'success'
         ORDER BY started_at DESC
         LIMIT 1
        """,
        (workflow_name,),
    )
    if row is None:
        return None, None

    previous_steps: dict[str, str] = {}
    for entry in row["output"] or []:
        if isinstance(entry, dict) and entry.get("success"):
            previous_steps[entry["name"]] = entry.get("output", "")
    return row["id"], previous_steps


async def run_workflow(
    workflow: Workflow,
    file_path: str,
    on_step_complete: Callable[[StepResult], None] | None = None,
    trigger_context: dict | None = None,
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

    previous_run_id, previous_steps = await _lookup_previous_run(workflow.name)
    planned_steps = [{"name": s.name, "type": s.type} for s in workflow.steps]

    await execute_query(
        """
        INSERT INTO workflow_runs
            (id, workflow_name, workflow_file_path, started_at, status,
             previous_run_id, planned_steps, trigger_context)
        VALUES (%s, %s, %s, %s, 'running', %s, %s, %s)
        """,
        (
            run_id,
            workflow.name,
            file_path,
            started_at,
            previous_run_id,
            json.dumps(planned_steps),
            json.dumps(trigger_context) if trigger_context is not None else None,
        ),
    )
    logger.info("Run %s started — workflow %r", run_id, workflow.name)

    results: dict[str, StepResult] = {}
    status = "success"
    error: str | None = None

    for step in workflow.steps:
        try:
            resolved = _resolve_step(step, results, previous_steps)
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
    # An ordered list — one entry per step that ran, in execution order.
    # A JSON object would not preserve order; the order is part of the data.
    output = [
        {"name": name, "success": r.success, "output": r.output} for name, r in results.items()
    ]

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
        previous_run_id=previous_run_id,
        planned_steps=planned_steps,
        trigger_context=trigger_context,
    )
