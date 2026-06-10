"""Executor for LLMStep — calls an OpenAI-compatible LLM endpoint."""

import httpx

from src.config import settings
from src.executors.base import StepResult, _failure, _success
from src.workflow.models import LLMStep

_LLM_TIMEOUT = 120.0


class LLMExecutor:
    """Runs an LLMStep against an OpenAI-compatible endpoint.

    Per-step fields on LLMStep override config defaults from settings:
    ``provider_url`` overrides LLM_BASE_URL, ``api_key`` overrides
    LLM_API_KEY, ``timeout_seconds`` overrides the default 120s timeout,
    ``max_tokens`` caps response length. Leave a field as None to fall
    back to the configured default.
    """

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

        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": step.temperature,
        }
        if step.max_tokens is not None:
            payload["max_tokens"] = step.max_tokens

        base_url = step.provider_url or settings.llm_base_url
        url = f"{base_url.rstrip('/')}/chat/completions"

        api_key = step.api_key if step.api_key is not None else settings.llm_api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        timeout = step.timeout_seconds if step.timeout_seconds is not None else _LLM_TIMEOUT

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return _failure(
                step.name,
                f"LLM endpoint returned {e.response.status_code}: {e.response.text[:300]}",
            )
        except httpx.HTTPError as e:
            return _failure(step.name, f"could not reach LLM endpoint at {url}: {e}")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            return _failure(step.name, f"unexpected LLM response shape: {e}")

        return _success(step.name, content)
