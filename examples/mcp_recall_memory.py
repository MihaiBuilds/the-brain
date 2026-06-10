"""Example workflow — recall memories via MCP, summarize, save.

Demonstrates ``McpToolStep`` calling Memory Vault's MCP server, then
chaining the result through ``{previous.X}`` into an ``LLMStep`` and a
``ShellStep``.

The ``server_command`` below assumes Memory Vault's MCP server is
available inside the container that runs this workflow. The stock
``the-brain`` image does NOT bundle any MCP server — that would couple
The Brain to a specific ecosystem product and violate the
stands-alone rule. Instead, you derive your own image:

    FROM mihaibuilds/the-brain:latest
    # install whatever MCP server(s) your workflows call —
    # see each server's repo for install instructions.

See the README "React to MCP tools" section for the full pattern.

Run it with:

    docker compose exec brain brain run examples/mcp_recall_memory.py
"""

from src.workflow import LLMStep, McpToolStep, ShellStep, Workflow

workflow = Workflow(
    name="mcp-recall-memory",
    steps=[
        McpToolStep(
            name="recall",
            server_command="python -m memory_vault.mcp",
            tool="recall",
            args={
                "query": "what happened this week",
                "space": "work",
                "limit": 10,
            },
            timeout_seconds=30.0,
        ),
        LLMStep(
            name="summarize",
            system="You write concise weekly digests.",
            prompt="Summarize these recalled memories into a short digest:\n\n{recall}",
        ),
        ShellStep(
            name="save",
            command="cat > weekly-digest.md",
        ),
    ],
)
