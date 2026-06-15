"""Tests for the StdioMcpClient — real subprocesses via mock_mcp_server fixture.

The fixture is a real Python subprocess that speaks the MCP stdio
transport with predictable behavior shaped by environment variables.
No mocking of asyncio or subprocess primitives — the only way to
verify subprocess lifecycle correctness is to actually spawn them.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from src.mcp import McpProtocolError, StdioMcpClient


def _mock_server_cmd() -> str:
    """Shell command that spawns the mock MCP server under the current interpreter."""
    return f"{sys.executable} -m tests.fixtures.mock_mcp_server"


# ---------------------------------------------------------------------------
# Connect + handshake
# ---------------------------------------------------------------------------


async def test_connect_runs_initialize_handshake():
    async with StdioMcpClient(_mock_server_cmd()) as client:
        # If we get here, handshake passed and notifications/initialized
        # was sent — the connection is live.
        assert client._handshake_done is True


async def test_connect_fails_when_server_returns_handshake_error(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_HANDSHAKE", "error")
    with pytest.raises(McpProtocolError, match="handshake failed"):
        async with StdioMcpClient(_mock_server_cmd()):
            pass


async def test_connect_fails_on_malformed_handshake_json(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_HANDSHAKE", "malformed-json")
    with pytest.raises(McpProtocolError, match="malformed JSON"):
        async with StdioMcpClient(_mock_server_cmd()):
            pass


async def test_connect_fails_on_handshake_id_mismatch(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_HANDSHAKE", "wrong-id")
    with pytest.raises(McpProtocolError, match="did not match"):
        async with StdioMcpClient(_mock_server_cmd()):
            pass


async def test_connect_fails_on_missing_result(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_HANDSHAKE", "no-result")
    with pytest.raises(McpProtocolError, match="missing 'result'"):
        async with StdioMcpClient(_mock_server_cmd()):
            pass


async def test_connect_fails_on_unspawnable_command():
    with pytest.raises(McpProtocolError, match="could not spawn"):
        async with StdioMcpClient("/nonexistent/path/to/mcp-server --does-not-exist"):
            pass


async def test_connect_fails_on_empty_command():
    with pytest.raises(McpProtocolError, match="empty server_command"):
        async with StdioMcpClient("   "):
            pass


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------


async def test_call_tool_returns_server_result():
    async with StdioMcpClient(_mock_server_cmd()) as client:
        result = await client.call_tool("hello", {}, timeout=5.0)
        assert isinstance(result, dict)
        assert result["isError"] is False
        assert result["content"][0]["text"] == "tool 'hello' ran"


async def test_call_tool_passes_arguments_through(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "echo-args")
    async with StdioMcpClient(_mock_server_cmd()) as client:
        result = await client.call_tool("echo", {"foo": "bar", "n": 42}, timeout=5.0)
        # The mock server echoes args as JSON in the content text.
        import json as _json

        echoed = _json.loads(result["content"][0]["text"])
        assert echoed == {"foo": "bar", "n": 42}


async def test_call_tool_surfaces_server_error(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "error")
    async with StdioMcpClient(_mock_server_cmd()) as client:
        with pytest.raises(McpProtocolError, match="tool failed"):
            await client.call_tool("x", {}, timeout=5.0)


async def test_call_tool_times_out_on_slow_server(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "slow")
    monkeypatch.setenv("MOCK_MCP_SLOW_SECONDS", "3.0")
    async with StdioMcpClient(_mock_server_cmd()) as client:
        with pytest.raises(McpProtocolError, match="timed out after 0.5s"):
            await client.call_tool("x", {}, timeout=0.5)


async def test_call_tool_fails_when_server_crashes(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_TOOL_BEHAVIOR", "crash")
    async with StdioMcpClient(_mock_server_cmd()) as client:
        with pytest.raises(McpProtocolError, match="closed stdout"):
            await client.call_tool("x", {}, timeout=5.0)


async def test_call_tool_before_connect_raises():
    client = StdioMcpClient(_mock_server_cmd())
    with pytest.raises(McpProtocolError, match="not connected"):
        await client.call_tool("x", {}, timeout=5.0)


# ---------------------------------------------------------------------------
# Stderr capture
# ---------------------------------------------------------------------------


async def test_stderr_is_captured_as_rolling_tail(monkeypatch):
    monkeypatch.setenv("MOCK_MCP_STDERR", "log line from server\n")
    async with StdioMcpClient(_mock_server_cmd()) as client:
        await client.call_tool("hello", {}, timeout=5.0)
        # Give the stderr reader a moment to drain.
        await asyncio.sleep(0.1)
        assert "log line from server" in client.stderr_tail


async def test_stderr_pipe_does_not_block_protocol_when_chatty(monkeypatch):
    # Server writes lots of stderr in a loop; client must NOT deadlock
    # waiting for stdout while the stderr pipe fills.
    monkeypatch.setenv("MOCK_MCP_STDERR", "x" * 100)
    monkeypatch.setenv("MOCK_MCP_STDERR_LOOP", "yes")
    async with StdioMcpClient(_mock_server_cmd()) as client:
        # The handshake itself already proved no deadlock. One tool call
        # confirms the protocol stays responsive under stderr pressure.
        result = await client.call_tool("hello", {}, timeout=5.0)
        assert result["isError"] is False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_close_is_idempotent():
    client = StdioMcpClient(_mock_server_cmd())
    await client.connect()
    await client.close()
    await client.close()  # second close must not raise


async def test_close_before_connect_is_safe():
    client = StdioMcpClient(_mock_server_cmd())
    await client.close()


async def test_context_manager_cleans_up_on_exception():
    proc_ref: list = []

    class _Marker(Exception):
        pass

    with pytest.raises(_Marker):
        async with StdioMcpClient(_mock_server_cmd()) as client:
            proc_ref.append(client._proc)
            raise _Marker

    # Subprocess must be reaped after the with-block unwinds.
    proc = proc_ref[0]
    assert proc.returncode is not None
