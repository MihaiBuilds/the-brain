"""Executor for MemoryVaultStep — queries Memory Vault over its REST API."""

from src.clients import MemoryVaultClient, MemoryVaultError
from src.config import settings
from src.executors.base import StepResult, _failure, _success
from src.workflow.models import MemoryVaultStep


class MemoryVaultExecutor:
    """Runs a MemoryVaultStep and returns the matching memory chunks."""

    async def execute(self, step: MemoryVaultStep) -> StepResult:  # type: ignore[override]
        client = MemoryVaultClient(
            base_url=settings.memory_vault_url,
            token=settings.memory_vault_token,
        )
        try:
            results = await client.search(
                query=step.query,
                space=step.space,
                limit=step.limit,
            )
        except MemoryVaultError as e:
            return _failure(step.name, str(e))

        # Output is the chunk text, joined — readable on its own and usable
        # as input to a following step.
        text = "\n\n".join(r.get("content", "") for r in results)
        return _success(step.name, text or "(no results)")
