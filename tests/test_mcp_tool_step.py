"""Tests for McpToolStep + McpToolExecutor + substitution boundaries.

Same hermetic mock_mcp_server fixture as test_stdio_mcp_client. Tests
that touch the runner use the in-process model_copy substitution path
to verify the dict-args branch without needing a database.
"""

from __future__ import annotations

import json
import sys

import pytest
from pydantic import ValidationError

from src.executors.base import StepResult, get_executor
from src.executors.mcp_tool import McpToolExecutor
from src.runner.runner import _resolve_step
from src.workflow.models import McpToolStep, ShellStep


def _mock_server_cmd() -> str:
    return f"{sys.executable} -m tests.fixtures.mock_mcp_server"


# ---------------------------------------------------------------------------
# Dispatch + model
# ---------------------------------------------------------------------------


def test_get_executor_dispatches_mcp_tool_step():
    step = McpToolStep(name="t", server_command="x", tool="recall")
    assert isinstance(get_executor(step), McpToolExecutor)


def test_mcp_tool_step_rejects_empty_required_fields():
    with pytest.raises(ValidationError):
        McpToolStep(name="t", server_command="", tool="recall")
    with pytest.raises(ValidationError):
        McpToolStep(name="t", server_command="x", tool="")


def test_mcp_tool_step_rejects_non_positive_timeout():
    with pytest.raises(ValidationError):
        McpToolStep(name="t", server_command="x", tool="recall", timeout_seconds=0.0)


# ---------------------------------------------------------------------------
# Executor — happy path + error handling against real mock server
# ---------------------------------------------------------------------------


async def test_mcp_executor_returns_serialized_content():
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="recall", timeout_seconds=5.0
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is True
    content = json.loads(result.output)
    assert content == [{"type": "text", "text": "tool 'recall' ran"}]


async def test_mcp_executor_passes_args_through(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "echo-args")
    step = McpToolStep(
        name="t",
        server_command=_mock_server_cmd(),
        tool="echo",
        args={"query": "hello", "limit": 5},
        timeout_seconds=5.0,
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is True
    content = json.loads(result.output)
    echoed = json.loads(content[0]["text"])
    assert echoed == {"query": "hello", "limit": 5}


async def test_mcp_executor_is_error_true_becomes_step_failure(monkeypatch):
    # Mock server doesn't have a direct "isError=true" mode; simulate by
    # using the JSON-RPC error response path which McpToolExecutor wraps
    # as a step failure too. Then verify the isError handler path
    # separately with a synthetic step result.
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "error")
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="recall", timeout_seconds=5.0
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is False
    assert "tool failed" in result.error


async def test_mcp_executor_times_out_on_slow_server(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "slow")
    monkeypatch.setenv("MOCK_MCP_SLOW_SECONDS", "3.0")
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="recall", timeout_seconds=0.5
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is False
    assert "timed out" in result.error


async def test_mcp_executor_fails_when_server_crashes(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "crash")
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="recall", timeout_seconds=5.0
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is False
    assert "closed stdout" in result.error


async def test_mcp_executor_fails_on_unspawnable_server():
    step = McpToolStep(
        name="t",
        server_command="/no/such/binary --whatever",
        tool="recall",
        timeout_seconds=5.0,
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is False
    assert "could not spawn" in result.error


# ---------------------------------------------------------------------------
# isError=true handler (direct unit test of the success → failure flip)
# ---------------------------------------------------------------------------


async def test_mcp_executor_treats_iserror_true_as_failure(monkeypatch):
    # Patch StdioMcpClient so call_tool returns a synthetic isError result
    # without going to a subprocess. This tests the executor's own
    # isError handling, separate from the JSON-RPC error path above.
    class _FakeClient:
        stderr_tail = ""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call_tool(self, *args, **kwargs):
            return {
                "content": [{"type": "text", "text": "tool said no"}],
                "isError": True,
            }

    monkeypatch.setattr("src.executors.mcp_tool.StdioMcpClient", _FakeClient)
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="recall", timeout_seconds=5.0
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is False
    assert "tool said no" in result.error


# ---------------------------------------------------------------------------
# Substitution boundaries — server_command + args string values substitute,
# tool name + args keys + non-string args values do NOT.
# ---------------------------------------------------------------------------


def _previous_results() -> dict[str, StepResult]:
    return {
        "first": StepResult(step_name="first", success=True, output="from-first")
    }


def test_resolve_step_substitutes_server_command():
    step = McpToolStep(
        name="t",
        server_command="cmd --flag {first}",
        tool="recall",
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, _previous_results(), None, None)
    assert resolved.server_command == "cmd --flag from-first"


def test_resolve_step_substitutes_args_string_values():
    step = McpToolStep(
        name="t",
        server_command=_mock_server_cmd(),
        tool="recall",
        args={"query": "echo {first}", "limit": 5},
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, _previous_results(), None, None)
    assert resolved.args["query"] == "echo from-first"
    assert resolved.args["limit"] == 5  # int passes through unchanged


def test_resolve_step_does_not_substitute_args_keys():
    # A {previous.X} token in an arg KEY must be treated as a literal
    # string key, never as a placeholder. Locked behavior per the M4
    # plan's "args keys are NOT substituted" rule.
    step = McpToolStep(
        name="t",
        server_command=_mock_server_cmd(),
        tool="recall",
        args={"{first}": "value"},  # key is a literal "{first}" string
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, _previous_results(), None, None)
    assert "{first}" in resolved.args  # key UNCHANGED
    assert "from-first" not in resolved.args  # no substitution happened on the key


def test_resolve_step_does_not_substitute_tool_name():
    # The `tool` field carries the MCP method name; it must never be
    # rewritten by placeholder substitution.
    step = McpToolStep(
        name="t",
        server_command=_mock_server_cmd(),
        tool="{first}",  # literal "{first}" as the tool name
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, _previous_results(), None, None)
    assert resolved.tool == "{first}"  # UNCHANGED


def test_resolve_step_leaves_args_non_string_values_unchanged():
    step = McpToolStep(
        name="t",
        server_command=_mock_server_cmd(),
        tool="recall",
        args={"n": 42, "ok": True, "nested": {"key": "{first}"}},
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, _previous_results(), None, None)
    assert resolved.args["n"] == 42
    assert resolved.args["ok"] is True
    # Nested dicts pass through verbatim — no recursive substitution
    # (locked v1.0 behavior, consistent with {trigger.body} no-nesting rule).
    assert resolved.args["nested"] == {"key": "{first}"}


# ---------------------------------------------------------------------------
# End-to-end: MCP step output flows into a downstream {previous.X} consumer.
# ---------------------------------------------------------------------------


def test_mcp_step_output_is_string_and_consumable_by_previous_x():
    # The MCP result is JSON-stringified to keep StepResult.output: str.
    # Verify a downstream ShellStep can interpolate {mcp_step} as a
    # string — the substitution machinery already handles this for any
    # step whose output is a string, but we pin the contract here so a
    # future "output: dict" experiment can't silently break {previous.X}.
    mcp_step_result = StepResult(
        step_name="recall",
        success=True,
        output='[{"type":"text","text":"hello"}]',
    )
    results = {"recall": mcp_step_result}
    downstream = ShellStep(name="echo", command="printf %s {recall}")
    resolved = _resolve_step(downstream, results, None, None)
    assert resolved.command == 'printf %s [{"type":"text","text":"hello"}]'
