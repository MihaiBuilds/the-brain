"""Executor for ShellStep — runs a shell command as a subprocess."""

import asyncio

from src.executors.base import StepResult, _failure, _success
from src.workflow.models import ShellStep


class ShellExecutor:
    """Runs a ShellStep, enforcing its timeout and capturing output."""

    async def execute(self, step: ShellStep) -> StepResult:  # type: ignore[override]
        try:
            proc = await asyncio.create_subprocess_shell(
                step.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return _failure(step.name, f"could not start command: {e}")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=step.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return _failure(step.name, f"command timed out after {step.timeout}s")

        stdout = stdout_b.decode(errors="replace").strip()
        stderr = stderr_b.decode(errors="replace").strip()

        if proc.returncode != 0:
            detail = stderr or stdout or "(no output)"
            return _failure(
                step.name,
                f"command exited with code {proc.returncode}: {detail}",
            )

        return _success(step.name, stdout)
