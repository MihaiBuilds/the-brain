"""Executor for McpToolStep — spawns an MCP server and invokes one tool."""

from __future__ import annotations

import json
import logging

from src.executors.base import StepResult, _failure, _success
from src.mcp import McpProtocolError, StdioMcpClient
from src.workflow.models import McpToolStep

logger = logging.getLogger(__name__)


class McpToolExecutor:
    """Runs an McpToolStep by spawning the configured MCP server.

    Per-step spawn lifecycle (D2 lock): one subprocess per step, killed
    at step end. Concurrent calls per client never happen.

    The MCP ``result.content`` array is JSON-stringified into
    ``StepResult.output`` so downstream ``{previous.X}`` consumers see
    a string (consistent with every other step type). Nested-JSON
    access via ``{previous.mcp_step.field}`` is NOT supported in v1.0
    — the output is a string after serialization, mirroring the
    ``{trigger.body}`` locked behavior.

    ``isError: true`` in the server response is treated as STEP failure
    (D4 lock) — the workflow halts the same way it would on a non-zero
    shell exit. Stderr from the MCP subprocess is captured to runner
    logs at step boundary, NOT returned in StepResult (D3 lock).
    """

    async def execute(self, step: McpToolStep) -> StepResult:  # type: ignore[override]
        try:
            async with StdioMcpClient(step.server_command) as client:
                try:
                    result = await client.call_tool(
                        step.tool, step.args, step.timeout_seconds
                    )
                except McpProtocolError as exc:
                    _log_stderr_tail(step, client.stderr_tail)
                    return _failure(step.name, str(exc))

                _log_stderr_tail(step, client.stderr_tail)
        except McpProtocolError as exc:
            return _failure(step.name, str(exc))

        if result.get("isError") is True:
            return _failure(step.name, _summarize_error(result))

        return _success(step.name, _serialize_content(result))


def _serialize_content(result: dict) -> str:
    """Render the MCP result.content array as a JSON string for {previous.X}."""
    content = result.get("content", [])
    return json.dumps(content, separators=(",", ":"))


def _summarize_error(result: dict) -> str:
    """Pull a readable error message out of an isError=true result."""
    content = result.get("content", [])
    if content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str) and text:
            return text
    return "MCP tool returned isError=true with no readable content"


def _log_stderr_tail(step: McpToolStep, tail: str) -> None:
    if tail:
        logger.info("MCP step %r stderr tail: %s", step.name, tail)
