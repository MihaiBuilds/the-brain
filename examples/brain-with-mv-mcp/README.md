# Derive-pattern example: The Brain + Memory Vault via MCP

This folder is a working end-to-end example of the derive-your-own-image pattern documented in the main README's "Call an MCP tool from a workflow" section. It composes The Brain with Memory Vault so a workflow can recall memories from MV over MCP stdio, summarize them with a local LLM, and write the result to a file — all inside one Docker network.

The Brain itself stays unchanged. Memory Vault stays unchanged. Both are independent products; this folder is how you compose them when you want that specific composition.

## What's in this folder

- **`Dockerfile`** — derives from the stock Brain image, adds Memory Vault's source under `/opt/memory-vault` and MV's runtime deps. MV is NOT pip-installed because its package name (`src`) collides with The Brain's own `src` package; the derived image keeps them isolated by path.
- **`docker-compose.yml`** — brings up three containers: `brain-db` (Brain's Postgres), `mv-db` (MV's Postgres with pgvector), and `brain-with-mv` (the derived image).
- **`verify_workflow.py`** — a 3-step workflow demonstrating `McpToolStep` → `LLMStep` → `ShellStep`. The MCP step's `server_command` spawns MV's MCP server with the right cwd, env vars, and Postgres connection settings scoped to the subprocess only.

## Quickstart

From the repo root:

```bash
# 1. Build the stock Brain image (so the derived Dockerfile has a FROM target)
docker compose build brain

# 2. Build the derived image and bring up the full stack
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml up -d --build

# 3. Apply MV's migrations to mv-db
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec -w /opt/memory-vault brain-with-mv \
    env DB_HOST=mv-db DB_PORT=5432 DB_NAME=memory_vault DB_USER=memory_vault DB_PASSWORD=memory_vault \
    python -c "import asyncio; from src.models.db import close_pool, init_pool, run_migrations; \
        asyncio.run((lambda: __import__('asyncio').gather(init_pool(), run_migrations(), close_pool()))())"

# 4. Create a space and ingest some sample content into MV
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec -w /opt/memory-vault brain-with-mv \
    env DB_HOST=mv-db DB_PORT=5432 DB_NAME=memory_vault DB_USER=memory_vault DB_PASSWORD=memory_vault \
    python -m src.cli space create work

docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec brain-with-mv \
    sh -c 'echo "# Notes\nThis is a sample memory." > /tmp/sample.md'

docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec -w /opt/memory-vault brain-with-mv \
    env DB_HOST=mv-db DB_PORT=5432 DB_NAME=memory_vault DB_USER=memory_vault DB_PASSWORD=memory_vault \
    python -m src.cli ingest /tmp/sample.md --space work

# 5. Run the verify workflow end-to-end
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec brain-with-mv \
    brain run examples/brain-with-mv-mcp/verify_workflow.py
```

`brain run` exits 0 on success. To see the run history and per-step outputs:

```bash
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec brain-with-mv brain history
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml exec brain-with-mv brain show <run-id>
```

## How the subprocess isolation actually works

The Brain image puts its source at `/app/src/`. MV's source is at `/opt/memory-vault/src/`. Both packages are literally named `src` — if both were on the same `sys.path` they would collide.

The fix is in `verify_workflow.py`'s `server_command`:

```python
sh -c 'cd /opt/memory-vault && export DB_HOST=mv-db ... && exec python -m src.mcp'
```

The `cd` changes the subprocess's working directory before Python launches. Python's default `sys.path[0]` is the cwd, so MV's `src/` resolves first when the MCP subprocess imports its own modules. The Brain parent process is unaffected — it keeps cwd `/app` and its own `src/`.

The `export` statements set MV's DB env vars (`DB_HOST=mv-db`, etc.) only inside the subprocess. The Brain parent process's env still has `DB_HOST=brain-db` pointing at its own Postgres.

This is the cleanest way we found to compose two products that both ship as `src.<module>` Python packages. Different products with different top-level package names would not need this dance.

## LLM model

The `LLMStep` in `verify_workflow.py` points at LM Studio on the host via `host.docker.internal:1234`. Load a small instruct model (Ministral-3B, Qwen2.5-7B-Instruct, Llama-3.2-3B-Instruct — anything that responds with visible content, not internal reasoning). Reasoning/thinking models can return empty `choices[0].message.content` because they consume their token budget on internal CoT.

## Cleanup

```bash
docker compose -f examples/brain-with-mv-mcp/docker-compose.yml down -v
```

The `-v` flag deletes the named volumes so a re-up starts fresh.
