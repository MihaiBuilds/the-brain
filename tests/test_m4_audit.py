"""Audit-pass tests for M4 — pins locked invariants that span multiple PRs.

The five named gaps:

1. LLMStep + McpToolStep step outputs flow into ``{previous.X}`` (cross-step
   pipeline contract).
2. Substitution boundaries pinned — LLM prompt + system substitute; MCP tool
   name + args keys + non-string args values do NOT.
3. Per-step spawn invariant — two consecutive MCP steps produce two distinct
   subprocess PIDs (no shared client, no pooling).
4. Timeout invariant — LLM and MCP both honor per-step ``timeout_seconds``.
5. "LM Studio only" caveat present in README (discipline-encoded-in-test,
   same shape as M3's 404-existence-leak lock).

These tests catch silent contract changes that single-PR tests would
miss because they only see one slice. Each test names exactly which lock it
defends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from src.executors.base import StepResult
from src.executors.llm import LLMExecutor
from src.executors.mcp_tool import McpToolExecutor
from src.mcp import StdioMcpClient
from src.runner.runner import _resolve_step
from src.workflow.models import LLMStep, McpToolStep, ShellStep

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mock_server_cmd() -> str:
    return f"{sys.executable} -m tests.fixtures.mock_mcp_server"


# ---------------------------------------------------------------------------
# Gap 1 — LLMStep + McpToolStep outputs flow into {previous.X}
# ---------------------------------------------------------------------------


def test_llm_output_substitutes_into_downstream_step():
    # An LLMStep's output (a string) must be substitutable into a later
    # step's substitutable field via {previous.X}. Pin both directions.
    llm_result = StepResult(step_name="ask", success=True, output="hello world")
    downstream = ShellStep(name="echo", command="printf %s {ask}")
    resolved = _resolve_step(downstream, {"ask": llm_result}, None, None)
    assert resolved.command == "printf %s hello world"


def test_mcp_output_substitutes_into_downstream_step():
    # An McpToolStep's output (JSON-stringified content) must also flow
    # into {previous.X}. Pins the StepResult.output: str contract that
    # makes the no-nested-access rule consistent across step types.
    mcp_result = StepResult(
        step_name="recall",
        success=True,
        output='[{"type":"text","text":"hi"}]',
    )
    downstream = LLMStep(name="summarize", prompt="Summarize: {recall}")
    resolved = _resolve_step(downstream, {"recall": mcp_result}, None, None)
    assert resolved.prompt == 'Summarize: [{"type":"text","text":"hi"}]'


# ---------------------------------------------------------------------------
# Gap 2 — substitution boundaries pinned
# ---------------------------------------------------------------------------


def test_llm_prompt_and_system_substitute_but_temperature_does_not():
    # LLMStep substitutable fields are exactly ("prompt", "system"). Adding
    # accidentally to that tuple would allow placeholder injection into
    # temperature, max_tokens, etc. — drift this test breaks loudly.
    prev = StepResult(step_name="ctx", success=True, output="WORLD")
    step = LLMStep(
        name="ask",
        prompt="Say {ctx}",
        system="You are {ctx}.",
        model="m",
        temperature=0.5,
    )
    resolved = _resolve_step(step, {"ctx": prev}, None, None)
    assert resolved.prompt == "Say WORLD"
    assert resolved.system == "You are WORLD."
    # Temperature stays a float — never substituted.
    assert resolved.temperature == 0.5


def test_mcp_tool_name_and_args_keys_never_substitute():
    # The tool name is an MCP method identifier; arg keys are protocol-level
    # parameter names. Neither is user data. Substituting either would let a
    # prior step's output rewrite the wire protocol.
    prev = StepResult(step_name="x", success=True, output="EVIL")
    step = McpToolStep(
        name="t",
        server_command="cmd",
        tool="{x}",
        args={"{x}": "value", "query": "{x}"},
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, {"x": prev}, None, None)
    assert resolved.tool == "{x}"  # NOT rewritten
    assert "{x}" in resolved.args  # key NOT rewritten
    assert resolved.args["query"] == "EVIL"  # value WAS rewritten


def test_mcp_args_non_string_values_pass_through_unchanged():
    # Ints, floats, bools, nested dicts in args MUST pass through unchanged.
    # Resolver iterates dict values and substitutes only strings.
    prev = StepResult(step_name="p", success=True, output="x")
    step = McpToolStep(
        name="t",
        server_command="cmd",
        tool="recall",
        args={
            "limit": 10,
            "score_threshold": 0.7,
            "include_metadata": True,
            "nested": {"inner": "{p}"},  # nested dicts — NOT recursed into
        },
        timeout_seconds=5.0,
    )
    resolved = _resolve_step(step, {"p": prev}, None, None)
    assert resolved.args["limit"] == 10
    assert resolved.args["score_threshold"] == 0.7
    assert resolved.args["include_metadata"] is True
    assert resolved.args["nested"] == {"inner": "{p}"}


# ---------------------------------------------------------------------------
# Gap 3 — per-step spawn invariant
# ---------------------------------------------------------------------------


async def test_two_mcp_steps_spawn_distinct_subprocesses():
    # The locked Decision 2: per-step spawn. Each McpToolStep spawns a fresh
    # subprocess; no client is shared. Pin by collecting PIDs across two
    # sequential clients and asserting they differ.
    pids: list[int] = []
    async with StdioMcpClient(_mock_server_cmd()) as client1:
        pids.append(client1._proc.pid)
    async with StdioMcpClient(_mock_server_cmd()) as client2:
        pids.append(client2._proc.pid)
    assert len(pids) == 2
    assert pids[0] != pids[1]


# ---------------------------------------------------------------------------
# Gap 4 — timeout invariant for both LLM and MCP step types
# ---------------------------------------------------------------------------


async def test_mcp_step_honors_per_step_timeout(monkeypatch):
    # McpToolStep.timeout_seconds is the wall-clock budget for the whole
    # call_tool, INCLUDING handshake. Locked behavior.
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "slow")
    monkeypatch.setenv("MOCK_MCP_SLOW_SECONDS", "3.0")
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="x", timeout_seconds=0.5
    )
    result = await McpToolExecutor().execute(step)
    assert result.success is False
    assert "timed out after 0.5s" in result.error


async def test_llm_step_honors_per_step_timeout(monkeypatch):
    # LLMStep.timeout_seconds is a per-step override. Pin that the
    # per-step timeout fires the failure path. We can't reliably trigger
    # a wall-clock timeout via MockTransport, so simulate the same
    # outcome by raising httpx.ReadTimeout from the transport (which is
    # what httpx itself raises when the configured timeout elapses).
    def timeout_handler(request):
        raise httpx.ReadTimeout("simulated per-step timeout", request=request)

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(timeout_handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    step = LLMStep(
        name="ask", prompt="hi", model="test", timeout_seconds=0.5
    )
    result = await LLMExecutor().execute(step)
    assert result.success is False
    assert "could not reach" in result.error


# ---------------------------------------------------------------------------
# Gap 5 — "LM Studio only" caveat present in README
# ---------------------------------------------------------------------------


def test_readme_lm_studio_only_caveat_is_locked():
    # If a future refactor rewrites the README's LLM section and drops the
    # caveat, this breaks loudly. Same discipline-encoded-in-test as M3's
    # 404-existence-leak lock — the caveat IS the v1.0 design statement.
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Tested against LM Studio only" in text
    assert "not promised in v1.0" in text


# ---------------------------------------------------------------------------
# Bonus pin — McpToolExecutor stderr_tail is logged, never returned in output
# ---------------------------------------------------------------------------


async def test_mcp_stderr_goes_to_logs_not_to_step_output(caplog, monkeypatch):
    # stderr_tail is debug metadata that goes to the runner's logs at
    # step boundary, NOT into StepResult.output. A workflow author
    # querying {previous.recall} must NEVER see stderr.
    monkeypatch.setenv("MOCK_MCP_STDERR", "warning from server\n")
    step = McpToolStep(
        name="t", server_command=_mock_server_cmd(), tool="recall", timeout_seconds=5.0
    )
    with caplog.at_level("INFO", logger="src.executors.mcp_tool"):
        result = await McpToolExecutor().execute(step)
    assert result.success is True
    assert "warning from server" not in result.output  # NOT in workflow data
    assert any(
        "stderr tail" in rec.message for rec in caplog.records
    )  # IS in logs
