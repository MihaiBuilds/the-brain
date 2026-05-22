# The Brain

Workflow orchestrator for the [MihaiBuilds](https://mihaibuilds.com) ecosystem. Connects [Memory Vault](https://github.com/MihaiBuilds/memory-vault), local LLMs, MCP tools, and shell commands into recurring workflows.

The Brain is a **workflow orchestrator, not an AI agent**. It doesn't make autonomous decisions — it runs Python-defined workflows you author, with full visibility into each step. The intelligence is in the workflow you write; The Brain is the runtime that makes it repeatable and observable.

## Roadmap

The Brain ships in five milestones. v1.0 is the full set, M1–M5.

| Milestone | Scope | Status |
|-----------|-------|--------|
| M1 — Bare Runner | Run Python-defined workflows, persist every run to Postgres, inspect run history from the CLI | ✅ Done |
| M2 — Triggers + State | Cron schedules, a long-running scheduler, workflows that read the previous run's output | 📋 Planned |
| M3 — Webhooks + File Watchers | Trigger workflows from HTTP webhooks and filesystem changes | 📋 Planned |
| M4 — MCP Tools + Multi-LLM | Call any MCP server as a workflow step; pluggable LLM providers | 📋 Planned |
| M5 — Polish + Launch | CI/CD, security pass, full docs, v1.0 release | 📋 Planned |

## What it will do (v1.0)

- Run workflows defined as Python files
- Steps can be: Memory Vault REST calls, local LLM calls (LM Studio), or shell commands
- Persistent run history in Postgres — every run logged with status and output
- Triggers: manual (CLI), cron, webhooks, file watchers
- Local LLM via LM Studio (OpenAI-compatible API)
- MCP tool calling — workflows can call any MCP server as a step

## What it won't do (v1.0)

- Multi-user / team workflows (PRO tier)
- Visual workflow builder (PRO tier)
- Rich conditional branching with parallel steps (PRO tier)
- Managed cloud version (PRO tier)

Single-tenant, self-hosted, MIT-licensed.

## Quickstart

This walks the runner end to end: install, configure, write a workflow, run it, inspect the result.

### 1. Prerequisites

- **Docker + Docker Compose** — runs Postgres and The Brain.
- **A running [Memory Vault](https://github.com/MihaiBuilds/memory-vault) instance** — only needed for workflow steps that query memory. Note its URL and an API token.
- **[LM Studio](https://lmstudio.ai)** — only needed for LLM steps. Load a model and start its local server.

A workflow that uses only shell steps needs neither Memory Vault nor LM Studio.

### 2. Install

```bash
git clone https://github.com/MihaiBuilds/the-brain.git
cd the-brain
docker compose up -d
```

This starts two containers: Postgres and The Brain. On boot, The Brain waits for the database, applies migrations automatically, and prints its status. The Brain container then stays alive so you can run CLI commands against it — at this stage there is no long-running service, the container's job is to host the `brain` CLI.

Check it came up cleanly:

```bash
docker compose exec brain brain status
```

You should see the database connection and one applied migration.

> After pulling new changes, rebuild the image with `docker compose up -d --build` — otherwise Compose reuses the previously built image and your update is not picked up.

### 3. Configure

Copy the example environment file and edit it:

```bash
cp .env.example .env
```

The database defaults work out of the box. For workflows that query Memory Vault, set:

- `MEMORY_VAULT_URL` — your Memory Vault instance. **The example file uses `http://localhost:8000`, which is correct only if you run the `brain` CLI directly on the host.** When The Brain runs in Docker (the flow above) and Memory Vault runs on the host, `localhost` points at the Brain's own container — use `http://host.docker.internal:8000` instead.
- `MEMORY_VAULT_TOKEN` — an API token from that Memory Vault instance. Create one with `docker compose exec app memory-vault token create the-brain` in the Memory Vault repo. Leave empty if that instance has auth disabled.

For LLM steps, set `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` to match your LM Studio server — and use `host.docker.internal` there too when running in Docker.

The database host port defaults to `5433` (set by `DB_PORT`) so it does not clash with a Postgres already on the host's `5432`.

After editing `.env`, recreate the containers so the new values take effect:

```bash
docker compose up -d
```

### 4. Run your first workflow

A workflow is a plain Python file that defines a module-level `workflow` variable. The repo ships [`examples/hello.py`](examples/hello.py) — two shell steps, no external services, so it runs straight after install:

```python
from src.workflow import ShellStep, Workflow

workflow = Workflow(
    name="hello",
    steps=[
        ShellStep(
            name="greeting",
            command="echo 'Hello from The Brain'",
        ),
        ShellStep(
            name="echo_it_back",
            command="echo 'The previous step said: {greeting}'",
        ),
    ],
)
```

A workflow is an ordered list of steps, run top to bottom. Steps pass data forward with **placeholders**: a `{step_name}` token in a later step's field is replaced with that earlier step's output. Here `{greeting}` is replaced with the first step's output. A placeholder that names no prior step fails that step rather than running with literal braces.

Run it:

```bash
docker compose exec brain brain run examples/hello.py
```

The Brain prints each step as it finishes and a final status line:

```
Running workflow 'hello' (2 steps)
  ✓ greeting
  ✓ echo_it_back
Run c609f5e0 — success
```

`brain run` exits `0` only if every step succeeds, and `1` on any failure — so it drops straight into a cron job or a CI pipeline.

### 5. A real-world workflow

[`examples/daily_digest.py`](examples/daily_digest.py) uses all three step types — it pulls recent memories, summarizes them with a local LLM, and writes the result to a file:

```python
from src.workflow import LLMStep, MemoryVaultStep, ShellStep, Workflow

workflow = Workflow(
    name="daily-digest",
    steps=[
        MemoryVaultStep(
            name="recent",
            query="what happened this week",
            space="work",
            limit=10,
        ),
        LLMStep(
            name="summarize",
            system="You write concise daily digests.",
            prompt="Summarize these memories into a short digest:\n\n{recent}",
        ),
        ShellStep(
            name="save",
            command="cat > digest.md",
        ),
    ],
)
```

The three step types:

- **`MemoryVaultStep`** — queries Memory Vault over its REST API.
- **`LLMStep`** — calls a local LLM through an OpenAI-compatible endpoint.
- **`ShellStep`** — runs a shell command.

This one needs a reachable Memory Vault and a configured LLM (see step 3). With both set up, run it the same way:

```bash
docker compose exec brain brain run examples/daily_digest.py
```

### 6. Inspect

Every run is persisted. List past runs, most recent first:

```bash
docker compose exec brain brain history
```

```
RUN       WORKFLOW                STATUS    STARTED               DURATION
c609f5e0  hello                   success   2026-05-22 19:54:58   0.0s
```

Show one run in full — pass the short ID from `run` or `history`:

```bash
docker compose exec brain brain show c609f5e0
```

```
Run:      c609f5e0-a8d6-4221-84c0-58c0b5d0460d
Workflow: hello
File:     examples/hello.py
Status:   success
Started:  2026-05-22 19:54:58.467911+00:00
Ended:    2026-05-22 19:54:58.494208+00:00
Duration: 0.0s

Steps:
  ✓ greeting
      Hello from The Brain
  ✓ echo_it_back
      The previous step said: Hello from The Brain
```

This prints the run's status, timing, and every step's output in execution order.

## Tech

- Python 3.11+
- PostgreSQL (single state engine across the ecosystem)
- Docker / Docker Compose for deployment
- Integrates with Memory Vault via its REST API

## License

MIT — see [LICENSE](LICENSE).
