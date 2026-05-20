"""Executor for LLMStep — calls a local LLM through an OpenAI-compatible API."""

import httpx

from src.config import settings
from src.executors.base import StepResult, _failure, _success
from src.workflow.models import LLMStep

_LLM_TIMEOUT = 120.0


class LLMExecutor:
    """Runs an LLMStep against an OpenAI-compatible endpoint (LM Studio)."""

    async def execute(self, step: LLMStep) -> StepResult:  # type: ignore[override]
        model = step.model or settings.llm_model
        if not model:
            return _failure(
                step.name,
                "no LLM model set — give the step a `model=` or set LLM_MODEL",
            )

        messages: list[dict[str, str]] = []
        if step.system:
            messages.append({"role": "system", "content": step.system})
        messages.append({"role": "user", "content": step.prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": step.temperature,
        }
        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return _failure(
                step.name,
                f"LLM endpoint returned {e.response.status_code}: "
                f"{e.response.text[:300]}",
            )
        except httpx.HTTPError as e:
            return _failure(step.name, f"could not reach LLM endpoint at {url}: {e}")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            return _failure(step.name, f"unexpected LLM response shape: {e}")

        return _success(step.name, content)
