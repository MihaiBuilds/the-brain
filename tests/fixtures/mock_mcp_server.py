"""Hermetic mock MCP server — a real subprocess used by client tests.

Speaks the MCP stdio transport (newline-delimited JSON-RPC 2.0).
Behavior is shaped by environment variables so tests can drive specific
code paths without modifying the script:

    MOCK_MCP_HANDSHAKE     "ok" (default), "error", "malformed-json",
                           "wrong-id", "no-result"
    MOCK_MCP_TOOL_BEHAVIOR "ok" (default), "error", "slow", "crash",
                           "echo-args"
    MOCK_MCP_SLOW_SECONDS  float — sleep time for "slow" tool behavior
    MOCK_MCP_STDERR        string written to stderr at startup
    MOCK_MCP_STDERR_LOOP   "yes" — write the stderr message in a loop
                           (used to test pipe-fill resilience)
    MOCK_MCP_EXIT_ON_CALL  "yes" — exit(1) on first tools/call

Invoked as ``python -m tests.fixtures.mock_mcp_server`` so the subprocess
runs under the same interpreter as the tests.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _write_raw(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _read() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _emit_initial_stderr() -> None:
    msg = os.environ.get("MOCK_MCP_STDERR", "")
    if not msg:
        return
    if os.environ.get("MOCK_MCP_STDERR_LOOP") == "yes":
        # Keep writing until killed — tests pipe-fill resilience.
        for _ in range(10000):
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
    else:
        sys.stderr.write(msg)
        sys.stderr.flush()


def _handle_initialize(request: dict) -> None:
    mode = os.environ.get("MOCK_MCP_HANDSHAKE", "ok")
    request_id = request.get("id")
    if mode == "ok":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-mcp", "version": "0.0.0"},
                },
            }
        )
    elif mode == "error":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "handshake failed"},
            }
        )
    elif mode == "malformed-json":
        _write_raw("{this is not valid json")
    elif mode == "wrong-id":
        _write(
            {
                "jsonrpc": "2.0",
                "id": 999999,  # deliberately mismatched
                "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
        )
    elif mode == "no-result":
        _write({"jsonrpc": "2.0", "id": request_id})
    else:
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"unknown MOCK_MCP_HANDSHAKE: {mode}",
                },
            }
        )


def _handle_tools_call(request: dict) -> None:
    behavior = os.environ.get("MOCK_MCP_TOOL_BEHAVIOR", "ok")
    request_id = request.get("id")
    params = request.get("params", {}) or {}
    args = params.get("arguments", {}) or {}

    if behavior == "ok":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": f"tool {params.get('name')!r} ran"}
                    ],
                    "isError": False,
                },
            }
        )
    elif behavior == "echo-args":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(args)}],
                    "isError": False,
                },
            }
        )
    elif behavior == "error":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "tool failed"},
            }
        )
    elif behavior == "slow":
        seconds = float(os.environ.get("MOCK_MCP_SLOW_SECONDS", "2.0"))
        time.sleep(seconds)
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": "slow ok"}]},
            }
        )
    elif behavior == "crash":
        sys.exit(2)
    else:
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"unknown MOCK_MCP_TOOL_BEHAVIOR: {behavior}",
                },
            }
        )


def main() -> None:
    _emit_initial_stderr()

    seen_initialize = False
    while True:
        try:
            request = _read()
        except json.JSONDecodeError:
            # Stay alive; let the client surface the protocol error.
            continue
        if request is None:
            return  # stdin closed — graceful exit

        method = request.get("method")

        if method == "initialize":
            _handle_initialize(request)
            seen_initialize = True
        elif method == "notifications/initialized":
            # No response expected for notifications.
            continue
        elif method == "tools/call":
            if not seen_initialize:
                request_id = request.get("id")
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32002,
                            "message": "tools/call before initialize",
                        },
                    }
                )
                continue
            if os.environ.get("MOCK_MCP_EXIT_ON_CALL") == "yes":
                sys.exit(1)
            _handle_tools_call(request)
        else:
            request_id = request.get("id")
            if request_id is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": f"method not implemented: {method}",
                        },
                    }
                )


if __name__ == "__main__":
    main()
