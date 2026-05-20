"""
Executor protocol, the per-step result, and dispatch by step type.

Each step type (memory_vault / llm / shell) has one executor. Executors
hold the behavior and I/O; the step models stay pure data. A caller
looks up an executor with ``get_executor`` and calls ``execute`` without
knowing the concrete step type.
"""

from typing import Protocol

from pydantic import BaseModel

from src.workflow.models import LLMStep, MemoryVaultStep, ShellStep, Step


class StepResult(BaseModel):
    """The outcome of running one step.

    ``output`` is the value a later step may read.
    """

    step_name: str
    success: bool
    output: str = ""
    error: str | None = None


class StepExecutor(Protocol):
    """Runs one step and returns its result. One implementation per step type."""

    async def execute(self, step: Step) -> StepResult: ...


def get_executor(step: Step) -> StepExecutor:
    """Return the executor for a step, dispatched on its type.

    Raises:
        ValueError: no executor is registered for the step type.
    """
    # Imported here to avoid a circular import — the concrete executors
    # import StepResult from this module.
    from src.executors.llm import LLMExecutor
    from src.executors.memory_vault import MemoryVaultExecutor
    from src.executors.shell import ShellExecutor

    if isinstance(step, MemoryVaultStep):
        return MemoryVaultExecutor()
    if isinstance(step, LLMStep):
        return LLMExecutor()
    if isinstance(step, ShellStep):
        return ShellExecutor()

    raise ValueError(f"no executor for step type: {type(step).__name__}")


def _failure(step_name: str, error: str) -> StepResult:
    """Build a failed StepResult — shared by the concrete executors."""
    return StepResult(step_name=step_name, success=False, error=error)


def _success(step_name: str, output: str) -> StepResult:
    """Build a successful StepResult — shared by the concrete executors."""
    return StepResult(step_name=step_name, success=True, output=output)
