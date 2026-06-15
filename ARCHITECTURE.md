# The Brain — Architecture Overview

This document describes the technical architecture of The Brain as it ships in **v1.0**. It covers the core components, the process model, how workflows execute, and the design decisions behind the trade-offs.

For user-facing setup and feature docs, see the [README](README.md). For contribution flow, see [CONTRIBUTING.md](CONTRIBUTING.md). For the threat model and security posture, see [SECURITY.md](SECURITY.md).

---

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         Operators                            │
│                                                              │
│   brain CLI       HTTP API client     Webhook sender         │
│   (one-shot)      (programmatic)      (HMAC-signed POST)     │
└──────┬────────────────────┬────────────────────┬─────────────┘
       │                    │                    │
       │           ┌────────▼─────────┐   ┌──────▼──────────┐
       │           │   brain-api      │   │   brain-api     │
       │           │  (FastAPI HTTP)  │   │  webhook route  │
       │           └────────┬─────────┘   └──────┬──────────┘
       │                    │                    │
       │                    └─────────┬──────────┘
       │                              │
       │           ┌──────────────────▼──────────────────┐
       │           │            Workflow Runner          │
       │           │                                     │
       │           │  Loader → Resolver → Step Executors │
       │           │              │                      │
       │           │      ┌───────┼──────────┐           │
       │           │      │       │          │           │
       │           │   Shell    LLM   MemoryVault   MCP  │
       │           │ subprocess HTTP    HTTP      stdio  │
       │           └───────────────────────────┬─────────┘
       │                                       │
       │           ┌──────────────────┐        │
       │           │   brain-watcher  │        │
       │           │  (file/cron daemon)│      │
       │           └─────────┬────────┘        │
       │                     │                 │
       └─────────────────────┼─────────────────┤
                             │                 │
                  ┌──────────▼─────────────────▼────────┐
                  │           PostgreSQL 16             │
                  │                                     │
                  │   workflow_runs                     │
                  │   webhook_secrets                   │
                  │   scheduler_state                   │
                  │   _migrations                       │
                  └─────────────────────────────────────┘
```

The same workflow runner is reachable from four equal first-class entry points: the **`brain` CLI** (one-shot manual runs), the **HTTP API** (programmatic dispatch + webhook ingestion), the **scheduler daemon** (cron-style triggers), and the **watcher daemon** (file-change triggers).

---

## Process Boundary

The Brain runs as three independent processes that share one database:

| Process | Started by | Responsibility |
|---|---|---|
| `brain run <file>` | CLI / operator | One-shot synchronous run of a single workflow |
| `brain serve` (a.k.a. `brain-api`) | Docker Compose `api` profile | FastAPI HTTP server — programmatic dispatch + webhook routes |
| `brain watch` (a.k.a. `brain-watcher`) | Docker Compose `watcher` profile | Long-running daemon that fires cron and file-change triggers |

**Why three processes, not one:** the runner is synchronous per-run, the API is async per-request, and the watcher is a long-running event loop. Splitting them keeps each process's failure mode independent — a crashing scheduler doesn't take the API down, and the operator can run only the parts they need (CLI-only, API-only, or full stack).

---

## Storage — PostgreSQL 16

Everything lives in a single Postgres database. Migrations are versioned and forward-only (`migrations/001_initial.sql`, `002_…`, …) and applied via the idempotent `brain migrate` command.

### Tables

**`workflow_runs`** — one row per execution.
- `id` UUID primary key
- `workflow_name` TEXT — name from the `Workflow(name=…)` definition
- `workflow_file_path` TEXT — absolute path of the source `.py` file
- `started_at`, `ended_at` TIMESTAMPTZ
- `status` TEXT — `running`, `success`, `failed`
- `output` JSONB — per-step result list (`name`, `success`, `output`, `error`)
- `error` TEXT — top-level error if the run failed before reaching the per-step loop

**`webhook_secrets`** — one row per registered webhook endpoint.
- `id` UUID primary key
- `workflow_name` TEXT
- `secret_hash` TEXT — SHA-256 of the issued secret (the plaintext is shown once at registration)
- `created_at`, `last_used_at`, `revoked_at`

Webhook secrets are 32 random bytes from `secrets.token_urlsafe`; verification uses `hmac.compare_digest` for constant-time comparison.

**`scheduler_state`** — one row per cron trigger, holding `last_fired_at` so a restart doesn't re-fire the same window.

**`_migrations`** — applied-migration tracking table used by `brain migrate`.

### Why one database

Run history, webhook secrets, and scheduler state all live in the same Postgres. No separate queue (Redis, RabbitMQ), no second backup story, one connection string. Postgres' `LISTEN`/`NOTIFY` is sufficient for the cross-process wakeup The Brain needs at v1.0 scale.

---

## Workflow Loader

A workflow is a Python file that defines a module-level `workflow = Workflow(...)` object. The loader (`src/workflow/loader.py`) reads the file, executes it as a Python module via `importlib.util`, and grabs the `workflow` attribute.

This means:

- **Workflow files are trusted Python.** They can `import` anything, call any local function, and have full access to the host. The Brain is not a sandbox — see [SECURITY.md](SECURITY.md) for the threat model.
- **Static analysis works.** Type checkers and editors see real Python, not a YAML DSL.
- **No new language.** Anything you can do in Python is available inside a step.

The loader is the only entry point that turns a file path into a `Workflow` object. The runner, the scheduler, the watcher, and the API all go through it.

---

## Step Types (v1.0)

Located in `src/executors/`. Each executor takes a step definition + a context dict (previous step outputs + trigger payload) and returns a result dict.

| Step | Executor | What it does |
|---|---|---|
| `ShellStep` | `shell.py` | Runs a shell command in a subprocess; captures stdout/stderr + exit code |
| `LLMStep` | `llm.py` | Calls an OpenAI-compatible chat API (default target: LM Studio); captures the assistant message |
| `MemoryVaultStep` | `memory_vault.py` | Calls a Memory Vault REST endpoint (`recall`, `ingest`, etc.) with the configured bearer token |
| `McpToolStep` | `mcp_tool.py` | Launches an MCP server over stdio, calls a tool, captures the result (including `isError`) |

### Per-step subprocess spawn

`ShellStep` always spawns a fresh subprocess per step. This is deliberate — it gives every step its own process boundary so a crashing shell command can't take the runner down, and so step-level resource limits land on the OS rather than the runner. The cost is one `fork`/`exec` per shell step; for the workflow shapes The Brain targets, that cost is in the noise.

`McpToolStep` follows the same pattern: a fresh `python -m <server>` process is spawned per step, the tool call goes over stdio, and the process exits when the step completes.

### Substitution model

Step definitions accept `{previous.<step_name>.<field>}` and `{trigger.<field>}` placeholders. The resolver runs after each step completes and before the next step starts — substitution is **textual, not eval'd Python**. The substitution boundary is locked by tests and documented in the [README](README.md).

`isError` is a first-class field on `McpToolStep` results — an MCP tool can succeed at the protocol level but report a tool-level error, and the runner treats that as a step failure.

---

## Trigger Types (v1.0)

| Trigger | Where it runs | What fires it |
|---|---|---|
| `manual` | CLI | `brain run <file>` |
| `cron` | `brain watch` daemon | A cron expression evaluated against the host clock; the scheduler persists `last_fired_at` in `scheduler_state` so a restart doesn't re-fire the same window |
| `webhook` | `brain serve` HTTP API | A POST to `/webhooks/<workflow>` with a valid HMAC signature header |
| `file` | `brain watch` daemon | `watchdog` observes a configured path; debounced file-change events dispatch a run |

All triggers go through the same loader → runner path. There is no second execution implementation per trigger type.

---

## HTTP API

FastAPI with bearer auth and an auto-generated OpenAPI page at `/docs`. Served at `http://localhost:8000` after `docker compose --profile api up`.

### Endpoints (v1.0)

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service + DB health (no auth) |
| `POST` | `/runs` | Dispatch a workflow run (auth required) |
| `GET` | `/runs/{id}` | Get run status + per-step output (auth required) |
| `POST` | `/webhooks/{workflow_name}` | Fire a webhook-triggered run (HMAC signature required) |

### Authentication

All endpoints except `/health` require a bearer token. The token is read from `THE_BRAIN_API_TOKEN` at startup; an unset variable fails fast with a clear error naming the variable.

Webhook routes use HMAC instead of the bearer token — each webhook has its own secret created via `brain register-webhook <workflow_name>`, shown once at registration, and stored as a SHA-256 hash.

### Error handling

The API returns generic error JSON on failure. Full traces go to structured logs only, correlated by `run_id`. The structured-logging configuration lives in `src/logging_config.py` and is wired up by both the API and the CLI on startup.

---

## CLI

A `brain` command-line tool ships in the same Docker image as the API. Used for migrations, status checks, one-shot runs, history inspection, webhook secret management, and the diagnostic bundler.

```bash
brain run <workflow.py>          # one-shot run of a workflow
brain history                    # list recent runs (--workflow, --status filters)
brain show <run-id>              # full per-step detail for a run
brain status                     # DB health (used as Docker healthcheck)
brain migrate                    # apply pending migrations (idempotent)
brain register-webhook <name>    # mint a webhook HMAC secret
brain serve                      # start the HTTP API (uvicorn)
brain watch                      # start the scheduler + file watcher daemon
brain diagnose                   # produce a redacted diagnostic zip
brain --version                  # print the installed version
```

The diagnostic bundler is the entry point for bug reports — see [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Docker Setup

The bundled `docker-compose.yml` defines three services and uses Compose **profiles** to let operators run only what they need.

```yaml
services:
  db:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data

  brain-api:
    profiles: [api]
    build: .
    command: brain serve
    depends_on:
      db: { condition: service_healthy }

  brain-watcher:
    profiles: [watcher]
    build: .
    command: brain watch
    depends_on:
      db: { condition: service_healthy }
```

- `docker compose up -d db` — Postgres only (for CLI-only / local-dev users)
- `docker compose --profile api up -d` — Postgres + HTTP API
- `docker compose --profile api --profile watcher up -d` — full stack

The image is a single-stage Python 3.11 runtime that pip-installs the package and exposes the `brain` command as the entrypoint. Released images are published to **`ghcr.io/mihaibuilds/the-brain`** for both `linux/amd64` and `linux/arm64`. The release workflow handles the multi-arch build on every `v*.*.*` tag.

### Derive your own image

The base image is intentionally minimal — no `git`, no `curl`, no shell tooling beyond `sh`. Workflows that need additional binaries should derive their own image:

```dockerfile
FROM ghcr.io/mihaibuilds/the-brain:1.0
RUN apt-get update && apt-get install -y git curl jq && rm -rf /var/lib/apt/lists/*
```

This keeps the base image small for users who don't need those tools, and makes the dependency surface for each operator explicit.

---

## Structured Logging

`src/logging_config.py` wires structlog to stdlib logging so existing `logger = logging.getLogger(__name__)` call sites Just Work. Two renderers are available, selected via `LOG_FORMAT`:

- `keyvalue` (default for the CLI) — human-readable console output
- `json` (default for Docker daemons) — one JSON object per line, ready for log aggregation

A `bind_run_id()` context manager attaches the run UUID as a structured field to every log line emitted inside a workflow run, so logs are filterable by run without grepping free-text strings.

---

## Design Decisions

**Why PostgreSQL, not Redis + Postgres?**
A single Postgres covers run history, webhook secrets, and scheduler state. `LISTEN`/`NOTIFY` is enough for cross-process wakeup at v1.0 scale. Adding a second datastore would double the backup story and the operational mental model for no win at this scale.

**Why three processes, not one?**
The runner is synchronous per-run, the API is async per-request, the watcher is a long event loop. Splitting them means a crashing scheduler doesn't take the API down, and operators can run only the parts they need via Compose profiles.

**Why trusted Python workflows, not a YAML DSL?**
A YAML DSL is a small language you have to learn, and any non-trivial workflow ends up needing escape hatches that turn back into Python. Workflows-as-Python means full editor support, full type checking, and zero new syntax — at the cost of trusting the workflow author the same way you trust any other file in your repo.

**Why per-step subprocess spawn for shell + MCP?**
A crashing shell command or MCP server shouldn't take the runner down. Per-step subprocess spawn is the simplest isolation that achieves that. The fork/exec cost is in the noise for the workflow shapes The Brain targets.

**Why LM Studio first for `LLMStep`?**
LM Studio's native API supports `reasoning="off"`, which is the only reliable way to suppress chain-of-thought from thinking-capable models. The OpenAI-compatible fallback (`/v1/chat/completions`) works too, but LM Studio gets the production-quality path in v1.0.

**Why MIT-licensed core?**
Your automation should belong to you, not a cloud platform. A genuinely-useful free tier (not crippleware) is what builds the community and trust. The PRO tier is operational/scale features that teams pay for — multi-user, hosted scheduler, secrets vault — not capabilities withheld from individual users.

---

## What's in v1.0

Python-defined workflows, four step types (shell / LLM / Memory Vault / MCP), four trigger types (manual / cron / webhook / file), a `brain` CLI, an HTTP API, a watcher daemon, structured logging with `run_id` binding, a diagnostic bundler, one-command Docker with Compose profiles, multi-arch images, MIT-licensed.

For honest v1.0 limitations, see the [Limitations section in the README](README.md#limitations).
