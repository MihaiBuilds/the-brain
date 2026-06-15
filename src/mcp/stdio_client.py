"""Asyncio JSON-RPC 2.0 client over MCP stdio transport.

The MCP stdio transport is newline-delimited JSON — one complete JSON
message per line, terminated by ``\\n`` on both stdin and stdout. No
Content-Length framing (that's the streamable-HTTP transport).

This client implements only what is needed to call tools on a spawned
MCP server: the ``initialize`` handshake and a single ``tools/call``
method. Resources, prompts, ``tools/list``, and server-initiated
notifications are intentionally out of scope.

Lifecycle is an async context manager — handshake runs eagerly on
enter, subprocess is killed on exit (including on exception paths).
For tests that need to manipulate the lifecycle mid-flight, ``connect``
and ``close`` are also exposed publicly.

Concurrency: a single ``StdioMcpClient`` serializes ``call_tool``
invocations via an internal lock. The McpToolStep per-step-spawn
lifecycle means concurrent calls per client never happen in normal
use; serialization is a paranoia guard.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections import deque
from typing import Any

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_NAME = "the-brain"
_CLIENT_VERSION = "0.1.0"

# Cap stderr in memory at ~1 KB. The plan locks a "last ~1KB" tail in
# StepResult.output for debuggability; capping here means even a chatty
# server can't drive the brain process OOM.
_STDERR_TAIL_BYTES = 1024

# Default timeout for the initial handshake. Tool calls have their own
# timeout supplied per-call.
_HANDSHAKE_TIMEOUT = 10.0


class McpProtocolError(Exception):
    """Raised when the MCP server returns malformed or unexpected output."""


class StdioMcpClient:
    """JSON-RPC 2.0 client over MCP stdio transport.

    Spawns ``server_command`` as a subprocess on ``connect`` /
    ``__aenter__``, runs the MCP ``initialize`` handshake, then is
    ready for ``call_tool``. ``close`` / ``__aexit__`` kills the
    subprocess and reaps the background stderr reader.
    """

    def __init__(self, server_command: str) -> None:
        self._server_command = server_command
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: deque[bytes] = deque()
        self._stderr_tail_size = 0
        self._stderr_reader_task: asyncio.Task[None] | None = None
        self._call_lock = asyncio.Lock()
        self._next_request_id = 1
        self._handshake_done = False

    async def __aenter__(self) -> StdioMcpClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        """Spawn the subprocess and run the MCP ``initialize`` handshake.

        Raises:
            McpProtocolError: subprocess could not be spawned, or the
                handshake response was malformed or absent within
                ``_HANDSHAKE_TIMEOUT`` seconds.
        """
        if self._proc is not None:
            raise McpProtocolError("client already connected")

        argv = shlex.split(self._server_command)
        if not argv:
            raise McpProtocolError("empty server_command")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise McpProtocolError(f"could not spawn MCP server: {exc}") from exc

        # Background reader prevents the subprocess from blocking on a
        # full stderr pipe (~64 KB on macOS) while we wait for stdout.
        self._stderr_reader_task = asyncio.create_task(self._consume_stderr())

        try:
            await asyncio.wait_for(self._handshake(), timeout=_HANDSHAKE_TIMEOUT)
        except TimeoutError as exc:
            await self.close()
            raise McpProtocolError(f"MCP handshake timed out after {_HANDSHAKE_TIMEOUT}s") from exc
        except McpProtocolError:
            await self.close()
            raise

        self._handshake_done = True

    async def close(self) -> None:
        """Kill the subprocess and reap the stderr reader.

        Safe to call multiple times. Safe to call before ``connect``.
        """
        proc = self._proc
        if proc is None:
            return

        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass

        if self._stderr_reader_task is not None:
            try:
                await asyncio.wait_for(self._stderr_reader_task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                self._stderr_reader_task.cancel()

        self._proc = None
        self._stderr_reader_task = None

    @property
    def stderr_tail(self) -> str:
        """The last ~1 KB of subprocess stderr, decoded best-effort."""
        return b"".join(self._stderr_tail).decode("utf-8", errors="replace")

    async def call_tool(
        self, tool_name: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Invoke ``tools/call`` on the connected MCP server.

        Returns the raw ``result`` field from the JSON-RPC response, which
        per the MCP spec contains ``content`` (a list of content blocks)
        and optionally ``isError``.

        Raises:
            McpProtocolError: client not connected, subprocess died,
                protocol error, server returned a JSON-RPC error, or
                ``timeout`` seconds elapsed before a complete response.
        """
        if self._proc is None or not self._handshake_done:
            raise McpProtocolError("client not connected — call connect() first")

        async with self._call_lock:
            try:
                return await asyncio.wait_for(self._do_call_tool(tool_name, args), timeout=timeout)
            except TimeoutError as exc:
                raise McpProtocolError(
                    f"MCP tool call '{tool_name}' timed out after {timeout}s"
                ) from exc

    async def _handshake(self) -> None:
        """Run ``initialize`` then send ``notifications/initialized``."""
        request_id = self._next_id()
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": _CLIENT_NAME,
                        "version": _CLIENT_VERSION,
                    },
                },
            }
        )

        response = await self._read_message()
        if not isinstance(response, dict):
            raise McpProtocolError(
                f"handshake: expected JSON object, got {type(response).__name__}"
            )
        if response.get("id") != request_id:
            raise McpProtocolError(
                f"handshake: response id {response.get('id')!r} did not match "
                f"request id {request_id!r}"
            )
        if "error" in response:
            err = response["error"]
            raise McpProtocolError(f"handshake: server returned error: {err}")
        if "result" not in response:
            raise McpProtocolError("handshake: response missing 'result' field")

        # Per MCP spec, client sends notifications/initialized AFTER the
        # initialize response is received.
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )

    async def _do_call_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id()
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
        )

        response = await self._read_message()
        if not isinstance(response, dict):
            raise McpProtocolError(
                f"tools/call: expected JSON object, got {type(response).__name__}"
            )
        if response.get("id") != request_id:
            raise McpProtocolError(
                f"tools/call: response id {response.get('id')!r} did not match "
                f"request id {request_id!r}"
            )
        if "error" in response:
            err = response["error"]
            raise McpProtocolError(f"tools/call returned error: {err}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError("tools/call: response 'result' field missing or not an object")
        return result

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Send one JSON-RPC message over stdin as a single newline-delimited line."""
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpProtocolError(f"MCP server stdin closed: {exc}") from exc

    async def _read_message(self) -> Any:
        """Read one newline-delimited JSON message from stdout."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            line = await self._proc.stdout.readline()
        except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
            raise McpProtocolError(f"MCP server stdout closed: {exc}") from exc

        if not line:
            # readline returns b"" on EOF.
            returncode = self._proc.returncode
            raise McpProtocolError(
                f"MCP server closed stdout unexpectedly (exit code: {returncode})"
            )

        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(
                f"MCP server sent malformed JSON: {exc}; line={line[:200]!r}"
            ) from exc

    async def _consume_stderr(self) -> None:
        """Continuously drain stderr to keep a rolling ~1 KB tail."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_tail.append(chunk)
                self._stderr_tail_size += len(chunk)
                while self._stderr_tail_size > _STDERR_TAIL_BYTES and len(self._stderr_tail) > 1:
                    dropped = self._stderr_tail.popleft()
                    self._stderr_tail_size -= len(dropped)
        except (asyncio.CancelledError, ConnectionResetError):
            return

    def _next_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id
