"""End-to-end ecosystem verify workflow — The Brain calls Memory Vault via MCP.

Run inside the brain-with-mv container after MV's migrations have been applied
and some sample memories ingested:

    docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec \\
        brain-with-mv brain run examples/brain-with-mv-mcp/verify_workflow.py

The McpToolStep's `server_command` sets PYTHONPATH and MV's DB env vars only
inside the spawned subprocess — the Brain parent process keeps its own
PYTHONPATH=/app and DB_HOST=brain-db pointing at Brain's own Postgres.
"""

from src.workflow import LLMStep, McpToolStep, ShellStep, Workflow

# The MCP server_command is one long shell line. `env` sets the subprocess env;
# `python -m src.mcp` then runs MV's MCP entry point with PYTHONPATH pointing
# at /opt/memory-vault (where MV's source lives in the derived image).
_MV_MCP_CMD = (
    "sh -c '"
    "cd /opt/memory-vault && "
    "export DB_HOST=mv-db DB_PORT=5432 "
    "DB_NAME=memory_vault DB_USER=memory_vault DB_PASSWORD=memory_vault && "
    "exec python -m src.mcp"
    "'"
)

workflow = Workflow(
    name="ecosystem-verify",
    steps=[
        McpToolStep(
            name="recall",
            server_command=_MV_MCP_CMD,
            tool="recall",
            args={
                "query": "what did I work on this week",
                "limit": 5,
            },
            timeout_seconds=60.0,
        ),
        LLMStep(
            name="summarize",
            system="You write short, plain summaries.",
            prompt="In two sentences, summarize what was recalled:\n\n{recall}",
            # Instruct model — fast, visible output immediately. Good
            # default for verify-by-following runs. Drop the model field
            # to use whatever LM_MODEL env var is set to globally.
            model="mistralai/ministral-3-3b",
            timeout_seconds=120.0,
            max_tokens=400,
            # Swap to a reasoning model for higher-quality output —
            # bump both budgets to give the reasoning phase room to
            # finish. Per-step overrides are the mechanism that makes
            # this swap a one-line change:
            #
            #     model="qwen/qwen3.5-9b",
            #     timeout_seconds=600.0,
            #     max_tokens=8000,
        ),
        ShellStep(
            name="save",
            command="echo '{summarize}' > /tmp/ecosystem-verify-output.txt && cat /tmp/ecosystem-verify-output.txt",
        ),
    ],
)
