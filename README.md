# The Brain

Workflow orchestrator for the [MihaiBuilds](https://mihaibuilds.com) ecosystem. Connects [Memory Vault](https://github.com/MihaiBuilds/memory-vault), local LLMs, MCP tools, and shell commands into recurring workflows.

The Brain is a **workflow orchestrator, not an AI agent**. It doesn't make autonomous decisions — it runs Python-defined workflows you author, with full visibility into each step. The intelligence is in the workflow you write; The Brain is the runtime that makes it repeatable and observable.

## Roadmap

The Brain ships in five milestones. v1.0 is the full set, M1–M5.

| Milestone | Scope | Status |
|-----------|-------|--------|
| M1 — Bare Runner | Run Python-defined workflows, persist every run to Postgres, inspect run history from the CLI | ✅ Done |
| M2 — Triggers + State | Cron schedules, a long-running scheduler, workflows that read the previous run's output | ✅ Done |
| M3 — Webhooks + File Watchers | Trigger workflows from HTTP webhooks and filesystem changes | ✅ Done |
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

## Tech

- Python 3.11+
- PostgreSQL (single state engine across the ecosystem)
- Docker / Docker Compose for deployment
- Integrates with Memory Vault via its REST API

## License

MIT — see [LICENSE](LICENSE).
