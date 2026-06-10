"""MCP (Model Context Protocol) client integration.

Exposes ``StdioMcpClient`` — a thin asyncio JSON-RPC 2.0 client that
speaks the MCP stdio transport. Used by ``McpToolStep`` to call tools
on an MCP server spawned as a subprocess.

Only the stdio transport is supported. Only the ``initialize`` handshake
and ``tools/call`` method are implemented; resources, prompts,
notifications-from-server, and ``tools/list`` are intentionally out of
scope.
"""

from src.mcp.stdio_client import McpProtocolError, StdioMcpClient

__all__ = ["StdioMcpClient", "McpProtocolError"]
