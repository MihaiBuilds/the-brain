# The Brain

**The workflow runtime for the [MihaiBuilds](https://mihaibuilds.com) ecosystem.** Self-hosted Postgres-backed scheduler, four trigger types (manual, cron, webhook, file), and four step types (shell, LLM, Memory Vault REST, MCP).

Every workflow you write today drifts into a one-off script tomorrow. Cron lines on a server you forgot. A bash file glued to a Python script glued to a webhook handler. State scattered across `.env` files, log files, and the database the script happens to know about. Repeatability is a checkbox; observability is `tail -f`.

The Brain is the runtime underneath. You define a workflow as a Python file with named steps. The Brain runs it — on demand, on a cron schedule, on an HTTP webhook, or on a filesystem change — and persists every run to Postgres so you can inspect what happened. Steps can shell out, call a local LLM, query [Memory Vault](https://github.com/MihaiBuilds/memory-vault), or invoke any MCP server. The Brain is a **workflow orchestrator, not an AI agent** — it doesn't make autonomous decisions, it runs the workflow you authored with full visibility into each step.

---

## Status

**v1.0 — released YYYY-MM-DD.** First stable release of The Brain. Five milestones (M1 bare runner, M2 triggers + state, M3 webhooks + file watchers, M4 MCP tools + multi-LLM, M5 polish + launch) all shipped and stable.

Release notes: [GitHub Releases](https://github.com/MihaiBuilds/the-brain/releases). Build-in-public story: the [mihaibuilds.com blog series](https://mihaibuilds.com/blog).

Semver from here forward — the public surface (CLI commands, workflow file format, step types, trigger contracts, DB schema) is stable. Breaking changes only on a major version bump.

---

## Quick Start (Docker)

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

This starts two containers: Postgres and The Brain. On boot, The Brain waits for the database, applies migrations, prints its status, and then runs the **scheduler daemon** — the long-running process that polls registered workflows every 10 seconds and fires the ones that are due. CLI commands run against the same container via `docker compose exec brain ...` as separate processes; they share the database with the daemon, no handshake needed.

Check it came up cleanly:

```bash
docker compose exec brain brain status
docker compose exec brain brain daemon-status
```

The first shows the database connection and applied migrations. The second confirms the daemon has ticked recently — it exits 0 when the daemon is healthy, 1 otherwise. Docker uses the same command as its healthcheck.

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

[`examples/daily_digest.py`](examples/daily_digest.py) uses three of the four step types — it pulls recent memories, summarizes them with a local LLM, and writes the result to a file:

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

### Step types reference

| Step type | What it does | Substitutable fields | Notes |
|---|---|---|---|
| **`MemoryVaultStep`** | Queries Memory Vault over its REST API. | `query` | Easy default for talking to MV — no derived image required. |
| **`LLMStep`** | Calls an OpenAI-compatible LLM endpoint. | `prompt`, `system` | Per-step overrides: `provider_url`, `api_key`, `model`, `timeout_seconds`, `max_tokens`, `temperature`. Tested against LM Studio only; other OpenAI-compatible providers may work via the same wire format but are not promised in v1.0. |
| **`ShellStep`** | Runs a shell command as a subprocess. | `command` | Captures stdout; non-zero exit fails the step. |
| **`McpToolStep`** | Spawns an MCP server over stdio and invokes one tool. | `server_command`, string values inside `args` | Per-step spawn — one subprocess per step. `tool` name and `args` keys are NEVER substituted. See "Call an MCP tool from a workflow" below. |

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

## No-Docker quick start

If you prefer running without Docker:

### Prerequisites

- Python 3.11+
- PostgreSQL 16+ (any database The Brain can connect to)

### Setup

```bash
# Clone
git clone https://github.com/MihaiBuilds/the-brain.git
cd the-brain

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your PostgreSQL credentials

# Run migrations
brain migrate

# Verify
brain status
```

### Usage

```bash
# Run a workflow
brain run examples/hello.py

# Inspect run history
brain history

# Show one run in full
brain show <run_id_prefix>
```

The scheduler daemon, file watcher daemon, and HTTP API are runnable the same way (`brain daemon`, `brain watcher`, `brain serve`) — each is a separate long-running process.

## Features

- **Run Python-defined workflows** from a CLI, persisted to Postgres so every run is inspectable later
- **Four trigger types** — manual (CLI), cron, HMAC-authenticated webhooks, filesystem watchers
- **Four step types** — shell, LLM (OpenAI-compatible), Memory Vault REST, MCP tool calling over stdio
- **Per-step LLM overrides** — pick a different provider URL, model, API key, timeout, or max tokens per step
- **Derive-your-own-image pattern** — compose The Brain with any MCP server (Memory Vault, GitHub MCP, Sentry MCP, your own) without bloating the stock image
- **Separate processes per surface** — scheduler daemon, file watcher daemon, and HTTP API each in its own container, so one crash doesn't take the others down
- **Single-tenant, self-hosted, MIT-licensed** — your data, your machine, your workflows

## Architecture

Three things are deliberate about this architecture:

- **One database for state.** Run history, schedules, webhook secrets, watcher registrations all live in Postgres. No separate queue, no separate state store, no Redis to operate.
- **Process boundary per trigger surface.** The scheduler daemon, the file watcher daemon, and the HTTP API are separate processes — each in its own container under its own compose profile. One crashes; the others keep running. Crash recovery is scoped to the surface that owns the run.
- **Per-step spawn for MCP.** The Brain doesn't host any long-running MCP server. Each `McpToolStep` spawns a fresh subprocess for the duration of one tool call, completes the MCP `initialize` handshake, calls one tool, and tears the subprocess down. Isolation per call: a crashing MCP server only kills one step.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the deeper walk-through (process model, recovery semantics, substitution model, MCP transport choice).

## Tech Stack

- **Python 3.11+** — async backend with psycopg 3
- **PostgreSQL** — single state engine across the ecosystem (workflow runs, schedules, webhook secrets, watcher registrations, daemon heartbeats)
- **Click** — the CLI surface
- **FastAPI + uvicorn** — the optional HTTP API for webhook triggers
- **watchdog** — file system event observation for the watcher daemon
- **MCP (Model Context Protocol)** — stdio transport for the `McpToolStep`
- **Docker / Docker Compose** — one-command deployment with multiple profiles (`api`, `watcher`)
- **Integrates with [Memory Vault](https://github.com/MihaiBuilds/memory-vault)** — either over its REST API (`MemoryVaultStep`) or over its MCP server (`McpToolStep` + derive-pattern)

## How It Works

### Workflow execution

A workflow is a Python file defining a module-level `workflow` variable: a name plus a linear list of steps. The Brain imports the file, runs the steps top to bottom, and persists every run to Postgres. Each step's output (a string) is available to downstream steps via the `{previous.step_name}` placeholder. A step failure halts the workflow — the remaining steps don't run, the run row is persisted with the failure visible in `brain show <run_id>`. There is no continue-on-error in v1.0; if a workflow needs partial success semantics, the step author writes them explicitly into the step.

### Trigger surfaces

The Brain ships four trigger types, each owned by a different process:

- **Manual** — `brain run path/to/workflow.py` runs the workflow once, on demand, in the current shell.
- **Cron** — the scheduler daemon polls `workflow_schedules` every 10 seconds, fires due workflows sequentially in its own process, and advances `next_run_at` after each fire. SIGTERM gracefully shuts down after the current workflow finishes.
- **Webhook** — the HTTP API exposes `POST /webhook/<name>` with HMAC-SHA256 signature verification. The `X-Brain-Signature: sha256=<hex>` header is GitHub-compatible. Webhooks are gated by an `THE_BRAIN_API_TOKEN` bearer at the boundary; the API refuses to start without one.
- **File watcher** — the watcher daemon observes registered directories via the `watchdog` library, debounces events on a 500ms window per `(workflow, path)`, and fires the registered workflow with the file path in `{trigger.path}`. One container, one daemon, one process per host.

All three persistent triggers (cron, webhook, file) share the same `workflow_runs` table. `brain history`, `brain show`, and `brain list-triggers` give a unified view across all four trigger types.

### Substitution model

Step output flows downstream via `{previous.X}` and `{trigger.X}` placeholders. The substitution is **string-only and flat** — no nested-field access, no recursion into dict values. `{previous.recall}` becomes the recall step's output as a single string; if a downstream step needs a JSON field, an upstream step writes it directly to its output. This keeps the contract simple and the substitution surface inspectable.

Two boundaries are sharp:

- `McpToolStep.tool` is **never substituted** — it's an MCP protocol method name, not user data. The workflow file is the orchestrator; the LLM doesn't decide which tool to call.
- `args` **keys** are never substituted — only values, and only string values. Non-string values (ints, bools, nested dicts) pass through unchanged.

These are pinned by tests so a future refactor can't quietly drop them.

## Run workflows on a schedule

The Quickstart above runs workflows on demand with `brain run`. The Brain also ships a long-running **scheduler daemon** that fires registered workflows on a cron schedule, with no extra setup — the daemon is already running as PID 1 inside the `brain` container, polling for due workflows every 10 seconds.

### 1. Confirm the daemon is healthy

```bash
docker compose exec brain brain daemon-status
```

```
healthy: last tick 8s ago (daemon d3c623efccae)
```

This exits `0` when the daemon ticked within the last 30 seconds. Docker uses the same command as its container healthcheck.

### 2. Register a workflow on a cron schedule

`brain register` takes a workflow file path and a standard 5-field cron expression. The schedule lives in Postgres next to the run history:

```bash
docker compose exec brain brain register examples/hello.py --cron "*/1 * * * *"
```

```
Registered 'hello' — next fire 2026-06-01 13:12:00 UTC
```

Registration validates the cron expression, loads the workflow file, and rejects duplicate names — no silent overwrite. Pass `--name X` to register under a different name (handy if you want the same workflow on two schedules).

### 3. List registered schedules

```bash
docker compose exec brain brain list
```

```
NAME                CRON            ENABLED   LAST RUN              NEXT FIRE             FILE
hello               */1 * * * *     yes       —                     2026-06-01 13:12:00   /app/examples/hello.py
```

The dash under `LAST RUN` means the workflow has not fired yet. `--enabled` / `--disabled` / `--workflow NAME` filter the list.

### 4. Wait for the daemon to fire it

The daemon polls every 10 seconds, so the first fire lands within ten seconds of the cron boundary. Re-running `brain list` after the next minute shows the schedule's `LAST RUN` populated, and `brain history` shows the run row alongside any `brain run` invocations from the Quickstart:

```bash
docker compose exec brain brain list
```

```
NAME                CRON            ENABLED   LAST RUN              NEXT FIRE             FILE
hello               */1 * * * *     yes       2026-06-01 13:12:05   2026-06-01 13:13:00   /app/examples/hello.py
```

```bash
docker compose exec brain brain history
```

```
RUN       WORKFLOW                STATUS    STARTED               DURATION
c2cae6be  hello                   success   2026-06-01 13:12:05   0.0s
```

### 5. Disable, enable, unregister

A schedule can be paused without deleting it (`brain disable <name>`) and brought back later (`brain enable <name>`). Both are idempotent — calling them on an already-in-target-state schedule succeeds silently. `brain unregister <name>` deletes the schedule row outright; past run rows are preserved.

```bash
docker compose exec brain brain disable hello
```

```
'hello' disabled.
```

```bash
docker compose exec brain brain unregister hello
```

```
Unregistered 'hello'.
```

### Workflows that read their previous run

A scheduled workflow can read the output of its prior successful run via the `{previous.<step_name>}` placeholder — useful for digests that build on themselves, or workflows that diff today's state against yesterday's. [`examples/scheduled_digest.py`](examples/scheduled_digest.py) demonstrates the pattern:

```python
LLMStep(
    name="summary",
    prompt=(
        "Yesterday's summary:\n{previous.summary}\n\n"
        "Today's memories:\n{recent}\n\n"
        "Write today's summary."
    ),
)
```

On the very first run there is no previous successful run, so `{previous.summary}` is unresolvable — that step fails with a clear error, same strict-by-design behavior as M1's intra-run `{step_name}` placeholder. Once one run has succeeded, every subsequent run sees its output.

### Daemon lifecycle

The daemon is just another process in the brain container — `docker compose stop brain` shuts it down gracefully (SIGTERM finishes the currently-running workflow before exit), and `docker compose start brain` brings it back. On boot, any `workflow_runs` row still in `running` status from a previous crash is recovered as a failed run with `error = "daemon restarted with run in progress"`, so the run history stays consistent.

## React to webhooks

`brain register-webhook` registers a workflow to fire on inbound HTTP requests. The endpoint is opt-in via the `api` compose profile and authenticated per-webhook with an HMAC-SHA256 secret — same shape as GitHub's `X-Hub-Signature-256`, so existing webhook senders work without translation.

### 1. Bring up the API profile

The HTTP API requires the `THE_BRAIN_API_TOKEN` environment variable (for the bearer-token `/run` endpoint — see Quickstart). Webhook endpoints have their own per-row HMAC auth, but the API service refuses to start without the bearer token set.

```bash
THE_BRAIN_API_TOKEN=any-value docker compose --profile api up -d
```

### 2. Register a webhook

```bash
docker compose exec brain brain register-webhook examples/webhook_handler.py
```

```
Registered webhook 'webhook-handler'.

Save this secret now — it cannot be retrieved later:

  6YwAVUNJ8SV068ziGwr1h4gS-BETcOJIg57uR_Kl6YQ

Sign the request body with HMAC-SHA256 and send the digest in the X-Brain-Signature header as `sha256=<hex>`.
```

The secret is printed **once**. Save it before you lose the terminal — there is no `brain show-webhook-secret` by design. If you do lose it, `brain unregister-webhook NAME` then `brain register-webhook` issues a fresh one.

### 3. Fire the webhook from curl

Compute the HMAC-SHA256 of the raw body with the secret, send the digest in `X-Brain-Signature` as `sha256=<hex>`:

```bash
SECRET=6YwAVUNJ8SV068ziGwr1h4gS-BETcOJIg57uR_Kl6YQ
BODY='{"hello":"world"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"

curl -s -X POST http://localhost:8001/webhook/webhook-handler \
    -H "X-Brain-Signature: $SIG" \
    -H "Content-Type: application/json" \
    -d "$BODY"
```

```
{"run_id":"369021ff-71d2-45ea-b14f-5d0df2a62777","status":"success","started_at":"2026-06-07T15:10:52.380299Z","ended_at":"2026-06-07T15:10:52.406936Z","duration_seconds":0.026637,"error":null}
```

The endpoint runs the workflow synchronously and returns the run metadata. A wrong signature returns `401`. An unknown workflow name returns `404` — same shape as a disabled webhook, so existence is not leaked through the response code.

### 4. The workflow reads the trigger context

The workflow at [`examples/webhook_handler.py`](examples/webhook_handler.py) reads the inbound body via the `{trigger.body}` placeholder:

```python
ShellStep(
    name="received",
    command="echo got event={trigger.event} body={trigger.body}",
)
```

Four trigger placeholders are available in any field that supports substitution:

- `{trigger.event}` — `"webhook"` for webhook-triggered runs, `"file"` for file-triggered runs.
- `{trigger.body}` — the inbound body. Parsed JSON objects are stringified deterministically (sorted keys). Non-JSON bodies pass through as the raw string.
- `{trigger.headers.X}` — case-insensitive HTTP header lookup. The header allowlist is `content-type`, `user-agent`, `x-github-event`, `x-github-delivery`, `x-stripe-event`, `x-event-key`. Sensitive headers (Authorization, the signature itself, cookies) are never exposed.
- `{trigger.path}` — the file path for file-triggered runs; empty string for webhooks.

Referencing a trigger token on a manually-run or cron-fired workflow (no trigger context) fails the step with a clear error.

### 5. Inspect the run

```bash
docker compose exec brain brain history --workflow webhook-handler
```

```
RUN       WORKFLOW                STATUS    STARTED               DURATION
369021ff  webhook-handler         success   2026-06-07 15:10:52   0.0s
```

### Lifecycle

`brain disable-webhook NAME` makes the endpoint respond `404` (same shape as nonexistent — existence is not leaked even when paused). `brain enable-webhook NAME` brings it back. Both are idempotent. `brain unregister-webhook NAME` deletes the registration; past run rows are preserved.

## React to file changes

`brain register-watcher` registers a workflow to fire on filesystem events. The watcher daemon runs in its own container behind the `watcher` compose profile, separate from the scheduler so a watcher crash does not kill cron schedules.

### 1. Bring up the watcher profile

```bash
docker compose --profile watcher up -d
```

The `brain-watcher` service mounts `./watched` from the host as `/data/watched` inside the container — adjust this in `docker-compose.yml` if your workflows monitor a different directory. The `register-watcher` command must be run **from inside the watcher container** because it validates the watched directory exists at registration time, and only the watcher container has the mapped volume.

### 2. Register a watcher

```bash
docker compose exec brain-watcher brain register-watcher examples/markdown_watcher.py \
    --path /data/watched --events modified
```

```
Registered watcher 'markdown-watcher' — watching /data/watched for modified.
```

`--events` accepts a comma-separated list from `created`, `modified`, `deleted` (e.g. `--events created,modified`). The default is `modified` only.

### 3. Trigger a file event

Write to a file inside the watched directory on your host. The watcher daemon picks up the row on its next 10-second sync and starts observing within a few seconds:

```bash
echo "first content" > ./watched/test.md
```

A 500ms debounce per (workflow, path) coalesces multiple filesystem events from a single editor save into one workflow run.

### 4. Inspect the run

```bash
docker compose exec brain brain history --workflow markdown-watcher
```

```
RUN       WORKFLOW                STATUS    STARTED               DURATION
aa82e67d  markdown-watcher        success   2026-06-07 15:11:46   0.0s
```

The workflow at [`examples/markdown_watcher.py`](examples/markdown_watcher.py) reads the changed file's path via `{trigger.path}`:

```python
ShellStep(
    name="noticed",
    command="echo file event={trigger.event} path={trigger.path}",
)
```

### Watcher lifecycle

`brain watcher-status` reports the watcher daemon's heartbeat health — it must be run **from inside the brain-watcher container** since the heartbeat is keyed by the watcher container's hostname:

```bash
docker compose exec brain-watcher brain watcher-status
```

```
healthy: last tick 7s ago (watcher ade05e01d190:watcher)
```

`brain disable-watcher NAME` pauses the watcher (the daemon tears down its observer on the next sync); `brain enable-watcher NAME` brings it back. `brain unregister-watcher NAME` deletes the row; past run rows are preserved.

## Call an MCP tool from a workflow

`McpToolStep` lets a workflow spawn any MCP server as a subprocess, call one tool on it, and feed the result into a downstream step. The Brain implements only the stdio transport in v1.0 — the same transport Memory Vault's MCP server uses, the same one Claude Desktop uses, the same one every reference implementation uses.

```python
from src.workflow import LLMStep, McpToolStep, ShellStep, Workflow

workflow = Workflow(
    name="mcp-recall-memory",
    steps=[
        McpToolStep(
            name="recall",
            server_command="python -m memory_vault.mcp",
            tool="recall",
            args={"query": "what happened this week", "limit": 10},
            timeout_seconds=30.0,
        ),
        LLMStep(
            name="summarize",
            prompt="Summarize:\n\n{recall}",
        ),
        ShellStep(name="save", command="cat > digest.md"),
    ],
)
```

`server_command` is the shell command that starts the MCP server over stdio. The Brain spawns it on each step run, completes the MCP `initialize` handshake, invokes one tool, then kills the subprocess. Per-step spawn — no shared server process, no cross-step state.

`{previous.recall}` interpolates the recall step's output (the MCP tool's `result.content` array, JSON-serialized) into the LLM prompt the same way any other step's output flows downstream. Nested-field access like `{previous.recall.content[0].text}` is NOT supported in v1.0 — the output is a string after serialization, matching the `{trigger.body}` rule.

### Substitution boundaries

`McpToolStep` substitutes `{previous.X}` and `{trigger.X}` placeholders in:

- `server_command` — same as `ShellStep.command`
- String values inside `args` — `args={"query": "{previous.X}"}` becomes the resolved string

It does NOT substitute:

- The `tool` name — that's an MCP method name, not user data
- `args` keys — only values are substituted, never keys
- Non-string `args` values — ints, bools, nested dicts pass through unchanged

### The derive-your-own-image pattern

The stock `mihaibuilds/the-brain` image bundles ZERO MCP servers. The Brain is a workflow orchestrator; MCP servers are independent products. Coupling them would force users into installing things they don't need — anyone who only wants shell + LLM + webhook workflows should pay zero MCP cost.

If your workflow's `server_command` calls an MCP server, install that server in a derived image:

```dockerfile
FROM mihaibuilds/the-brain:latest

# Install whatever MCP server(s) your workflows call.
# Each server's install instructions live in its own repo —
# see Memory Vault, GitHub MCP, Sentry MCP, etc.
RUN <install-command-per-the-server-s-readme>
```

This keeps each ecosystem product independent. The Brain stands alone. Memory Vault stands alone. You compose them by deriving your own image with the combination you want.

Tested against Memory Vault's MCP server via this pattern. Other MCP servers should work with the same shape but are not promised in v1.0.

### `MemoryVaultStep` vs `McpToolStep` — which to use for Memory Vault?

Both can query Memory Vault. They optimize for different audiences:

| | `MemoryVaultStep` | `McpToolStep` |
|---|---|---|
| Transport | REST API over HTTP | MCP stdio (JSON-RPC 2.0) |
| Setup | Point at a running Memory Vault HTTP endpoint (`MEMORY_VAULT_URL`) | Install MV's MCP server in your derived image |
| Best for | Workflows that only want hybrid search from MV | Workflows that want any of MV's MCP tools (recall, remember, forget, memory_status) — or that already use other MCP servers |
| Available in stock image | Yes | No (requires derive-pattern) |

`MemoryVaultStep` is the no-extra-setup default for the common case (search MV from a workflow). `McpToolStep` is the generic any-MCP-server mechanism. Both ship in v1.0; neither is deprecated. Pick whichever fits.

## Trigger types

The Brain ships v1.0 with the four classical trigger types:

| Type | Started by | Command to register | Trigger context |
|------|------------|---------------------|-----------------|
| Manual | You, on demand | (none — `brain run path/to/workflow.py`) | none |
| Cron | The scheduler daemon | `brain register PATH --cron "EXPR"` | none |
| Webhook | An inbound HTTP request | `brain register-webhook PATH` | `event=webhook`, `body`, `headers`, `path=null` |
| File watcher | A filesystem event | `brain register-watcher PATH --path DIR --events ...` | `event=file`, `body=null`, `headers={}`, `path` |

`brain list-triggers` shows every registration across the three persistent trigger types in one table:

```bash
docker compose exec brain brain list-triggers
```

```
TYPE      NAME                    ENABLED   DETAIL
webhook   webhook-handler         yes       —
file      markdown-watcher        yes       /data/watched
```

Cron schedules from `brain register` appear here too once registered.

## Workflow lifecycle hooks

The Brain has a small number of named extension points worth knowing about. Each is documented in detail elsewhere; this section names them in one place so the inspectable surface is visible.

- **Per-step spawn lifecycle (`McpToolStep`)** — every MCP step spawns its server subprocess at step start, runs the MCP `initialize` handshake eagerly, invokes one `tools/call`, and kills the subprocess at step end. No shared client. No pooling. The per-step timeout covers handshake + call from the caller's perspective. See [Call an MCP tool from a workflow](#call-an-mcp-tool-from-a-workflow).
- **`{previous.X}` substitution boundary** — string-typed step outputs flow into downstream string fields via `{previous.step_name}`. Nested-field access (e.g. `{previous.X.foo.bar}`) is NOT supported in v1.0 — the value is a flat string after serialization. See [Substitution boundaries](#substitution-boundaries).
- **`{trigger.X}` substitution boundary** — webhook/file-watcher payloads flow into workflow steps via `{trigger.event}`, `{trigger.body}`, `{trigger.headers.X}`, `{trigger.path}`. Workflows triggered manually or by cron fail with a clear error if they reference `{trigger.X}`. See [React to webhooks](#react-to-webhooks) and [React to file changes](#react-to-file-changes).
- **`tool` name + `args` keys are NEVER substituted** — only string values inside `args` substitute. The MCP method name and argument schema are protocol-level identifiers, not user data. Locked behavior; tested.
- **`isError: true` → step failure** — when an MCP server returns a JSON-RPC success containing `isError: true` in its result, The Brain treats it as a failed step (same as a non-zero shell exit code). The first text content block becomes the step's error message.
- **Non-zero shell exit → step failure** — `ShellStep` captures stdout into `StepResult.output`; non-zero exit fails the step and halts the workflow.
- **First failure halts the workflow** — no continue-on-error in v1.0. The remaining steps don't run. The run row is persisted with the failure visible in `brain show <run_id>`.
- **stderr is logged, not in `StepResult.output`** — MCP server stderr is captured to a rolling ~1 KB tail and logged at step boundary. Workflow data and debug data are different surfaces; a `{previous.X}` reference never includes stderr noise.

## Troubleshooting

When something goes wrong, `brain diagnose` bundles your environment, version, status, and recent logs into a single zip suitable for attaching to a bug report:

```bash
brain diagnose
```

The bundle lands in the current directory as `brain-diagnostic-YYYY-MM-DD-HHMMSS.zip`. It includes a `MODE.txt` marker (Docker vs. no-Docker), `os_info.txt`, `brain_version.txt`, the output of `brain status`, filtered non-secret environment variables, and (under Docker) the last 1000 lines of the brain container and the last 500 lines of the db container.

Secrets are redacted by an allow-list — `DB_PASSWORD`, `LLM_API_KEY`, `MEMORY_VAULT_TOKEN`, and `THE_BRAIN_API_TOKEN` values are never written into the bundle; only their presence is recorded. The bundle ships with a `REDACTED_FIELDS.txt` explainer.

Logs are included unfiltered. If a workflow has logged a secret to stdout, that string will be in the bundle. **Review every file in the zip before posting it to a public issue tracker.**

Structured JSON logging is on by default in the Docker containers (`LOG_FORMAT=json`). Set `LOG_FORMAT=keyvalue` to switch to human-readable output when tailing logs locally.

## Limitations

### What v1.0 doesn't ship

Scope locked deliberately — these are not on the v1.0 roadmap, and may come in a later release if there's real demand signal:

- Multi-user / team workflows
- Visual workflow builder
- Rich conditional branching with parallel steps
- Managed cloud version

### Honest v1.0 limits

What v1.0 doesn't do well, named openly so you find out before deploying:

- **Reasoning-style LLMs need bigger budgets.** Default `timeout_seconds=60` and the LLM's own default `max_tokens` fail on qwen 3.x+ / o1-style / R1-style / QwQ models — they consume token budget on internal reasoning before producing visible content. Set `timeout_seconds=600` and `max_tokens=8000+` for a 9B reasoning model. Instruct models (Ministral, Mistral Instruct, Llama Instruct) don't have this behavior.
- **MCP stdio transport only.** HTTP transport (the streamable-HTTP MCP variant) is not in v1.0. Brings its own auth surface (Bearer / mTLS / OAuth) that v1.0 doesn't address.
- **Single-instance scheduler + watcher per host.** No HA. If the daemon dies, the workflows it schedules don't fire until it's back. Docker healthchecks (`brain daemon-status`, `brain watcher-status`) let your orchestrator restart it.
- **Workflow files are trusted Python imports.** The Brain `import`s your workflow file; arbitrary top-level Python runs at load time. v1.0's security posture is "workflow authors are trusted; don't run untrusted workflow files." This is not a sandbox.
- **LM Studio is the only LLM provider tested for v1.0.** Other OpenAI-compatible providers (Ollama, vLLM, llama.cpp server, OpenAI proper) may work via the same wire format but are not promised in v1.0.
- **English-only error messages.** No i18n.
- **No nested-field substitution.** `{previous.X.foo}` and `{trigger.body.foo}` are not path walks — the value is a flat string. If you need to pluck a field, do it in an upstream step that emits the field directly.
- **No `tools/list` MCP discovery.** Workflow authors are expected to know the tool name and args shape in advance, the same way they know what shell commands they're calling.
- **No per-run MCP server pooling.** Per-step spawn is the lifecycle. Two `McpToolStep` calls to the same server in one run produce two distinct subprocess PIDs and pay the cold-start cost twice.

## PRO tier (planned)

Team features, advanced workflow shapes (parallel branches, conditional fan-out), a managed/hosted tier. The free / open-source core stays free forever — open-core, not bait-and-switch.

A PRO tier ships only if there's real demand signal after v1.0.

## FAQ

### How is this different from n8n / Temporal / Airflow / Dagster?

Different shape, different audience.

- **n8n** is a visual node-based workflow tool aimed at non-developers integrating SaaS apps. The Brain is code-first (workflows are Python files) and aimed at developers who want a self-hosted runtime they can read end to end.
- **Temporal** is a distributed durable-execution engine with workers, activities, signals, and replay semantics. Powerful and large. The Brain is single-host, in-process, single-tenant — closer to "cron with state and triggers" than to a distributed workflow engine.
- **Airflow** is a DAG scheduler for data pipelines with operators, executors, and a web UI. Heavy. The Brain has no DAG (workflows are linear), no executors abstraction, no web UI.
- **Dagster** is asset-oriented data orchestration with strong typing for pipeline outputs. The Brain is generic-script-oriented — shell, LLM, MCP tool, REST call — not data-asset-oriented.

If you're building a data warehouse, use Airflow or Dagster. If you're orchestrating a SaaS automation by clicking nodes, use n8n. If you're running a distributed business process, use Temporal. If you're a developer who wants a small, observable, self-hosted runtime for "this Python workflow on a cron / webhook / file event" — that's The Brain.

### Do I need to know Python to use it?

Yes. Workflows are Python files defining a module-level `workflow` variable. You don't need deep Python; the workflow file is a list of dataclass-shaped steps. But you do need to be comfortable writing and editing Python.

### Can I use The Brain without Memory Vault or without an LLM?

Yes to both.

- **Without Memory Vault:** drop `MemoryVaultStep` and `McpToolStep` (for MV's MCP server). The Brain still runs shell + LLM workflows on all four trigger types.
- **Without an LLM:** drop `LLMStep`. The Brain still runs shell workflows, MCP tool workflows, and Memory Vault REST workflows on all four trigger types.

Each integration is optional. The stock image bundles ZERO MCP servers and assumes no LLM is running by default — both are opt-in via configuration.

### What does "workflow orchestrator, not agent" actually mean?

The workflow file is the orchestrator. The LLM step just transforms text. The LLM does NOT decide which tool to call, which step to run next, or when to stop — that's all in the workflow file. If a workflow wants "LLM picks an MCP tool," it wires that explicitly via `{previous.X}` substitution into an argument; the `tool` name itself is locked NOT-substituted. The agentic glue, if any, is the workflow author's code.

This is by design. The Brain ships an inspectable runtime, not a black box.

### Is this production-ready?

v1.0 is the first stable release. The runtime, scheduler, watcher, and API all run with a hermetic test suite (300+ tests against real Postgres, no mocks at integration boundaries) and a real end-to-end ecosystem-integration test against Memory Vault. The semver promise on the public surface holds from v1.0 forward.

"Production-ready" is a marketing claim a single maintainer can't make for someone else's environment. What's true: v1.0 is what The Brain shipped with, the failure modes are documented, the limitations are honest, and the discipline that built M1-M4 ships with M5.

### Can multiple users share workflows in v1.0?

No. v1.0 is single-tenant: one Postgres database per deployment, one shared scheduler, one shared watcher, one shared API token. Team features (per-user workflows, per-user run history, RBAC, per-user webhook secrets) are a candidate for a future PRO tier.

## License

MIT — see [LICENSE](LICENSE).

## Follow the Build

- Website: [mihaibuilds.com](https://mihaibuilds.com)
- Blog: [mihaibuilds.com/blog](https://mihaibuilds.com/blog)
- GitHub: [@MihaiBuilds](https://github.com/MihaiBuilds)
- X: [@mihaibuilds](https://x.com/mihaibuilds)

> Watch the repo to follow along.
