"""Tests for the three step executors.

The Memory Vault and LLM executors talk to HTTP services; those responses
are faked with ``httpx.MockTransport`` so the suite is hermetic and fast.
The shell executor runs real subprocesses — they are cheap and the real
behavior (exit codes, timeouts, output capture) is the thing worth testing.
"""

import httpx

from src.executors.base import get_executor
from src.executors.llm import LLMExecutor
from src.executors.memory_vault import MemoryVaultExecutor
from src.executors.shell import ShellExecutor
from src.workflow.models import LLMStep, MemoryVaultStep, ShellStep


def _mock_httpx(monkeypatch, handler):
    """Patch httpx.AsyncClient so every request is served by ``handler``.

    ``handler`` is a callable taking an httpx.Request and returning an
    httpx.Response — the standard MockTransport contract.
    """
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_get_executor_dispatches_by_step_type():
    assert isinstance(get_executor(MemoryVaultStep(name="q", query="x")), MemoryVaultExecutor)
    assert isinstance(get_executor(LLMStep(name="g", prompt="x")), LLMExecutor)
    assert isinstance(get_executor(ShellStep(name="s", command="echo x")), ShellExecutor)


# ---------------------------------------------------------------------------
# ShellExecutor — real subprocesses
# ---------------------------------------------------------------------------


async def test_shell_success_captures_stdout():
    result = await ShellExecutor().execute(ShellStep(name="s", command="echo hello"))
    assert result.success is True
    assert result.output == "hello"


async def test_shell_nonzero_exit_is_a_failure():
    result = await ShellExecutor().execute(ShellStep(name="s", command="exit 3"))
    assert result.success is False
    assert "code 3" in result.error


async def test_shell_failure_reports_stderr():
    result = await ShellExecutor().execute(ShellStep(name="s", command="echo oops >&2; exit 1"))
    assert result.success is False
    assert "oops" in result.error


async def test_shell_timeout_is_a_failure():
    result = await ShellExecutor().execute(ShellStep(name="s", command="sleep 5", timeout=1))
    assert result.success is False
    assert "timed out" in result.error


# ---------------------------------------------------------------------------
# MemoryVaultExecutor — mocked HTTP
# ---------------------------------------------------------------------------


async def test_memory_vault_success_joins_chunk_content(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"results": [{"content": "first chunk"}, {"content": "second chunk"}]},
        )

    _mock_httpx(monkeypatch, handler)
    result = await MemoryVaultExecutor().execute(MemoryVaultStep(name="q", query="x"))
    assert result.success is True
    assert result.output == "first chunk\n\nsecond chunk"


async def test_memory_vault_no_results_yields_placeholder(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"results": []})

    _mock_httpx(monkeypatch, handler)
    result = await MemoryVaultExecutor().execute(MemoryVaultStep(name="q", query="x"))
    assert result.success is True
    assert result.output == "(no results)"


async def test_memory_vault_http_error_is_a_failure(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="server exploded")

    _mock_httpx(monkeypatch, handler)
    result = await MemoryVaultExecutor().execute(MemoryVaultStep(name="q", query="x"))
    assert result.success is False
    assert "500" in result.error


# ---------------------------------------------------------------------------
# LLMExecutor — mocked HTTP
# ---------------------------------------------------------------------------


async def test_llm_success_returns_message_content(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "a tidy summary"}}]},
        )

    _mock_httpx(monkeypatch, handler)
    result = await LLMExecutor().execute(LLMStep(name="g", prompt="hi", model="test-model"))
    assert result.success is True
    assert result.output == "a tidy summary"


async def test_llm_without_a_model_fails_fast():
    # No model on the step and LLM_MODEL is unset in the test env — fail
    # before any HTTP call is attempted.
    from src.config import settings

    assert settings.llm_model == ""  # guards the premise of this test
    result = await LLMExecutor().execute(LLMStep(name="g", prompt="hi"))
    assert result.success is False
    assert "no LLM model" in result.error


async def test_llm_http_error_is_a_failure(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="model not loaded")

    _mock_httpx(monkeypatch, handler)
    result = await LLMExecutor().execute(LLMStep(name="g", prompt="hi", model="test-model"))
    assert result.success is False
    assert "503" in result.error


async def test_llm_unexpected_response_shape_is_a_failure(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    _mock_httpx(monkeypatch, handler)
    result = await LLMExecutor().execute(LLMStep(name="g", prompt="hi", model="test-model"))
    assert result.success is False
    assert "unexpected LLM response shape" in result.error
