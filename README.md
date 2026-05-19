# The Brain

> **Status: pre-alpha, in active development.** Not ready for use. Watch the repo for the v1.0 release.

Workflow orchestrator for the [MihaiBuilds](https://mihaibuilds.com) ecosystem. Connects [Memory Vault](https://github.com/MihaiBuilds/memory-vault), local LLMs, MCP tools, and shell commands into recurring workflows.

The Brain is a **workflow orchestrator, not an AI agent**. It doesn't make autonomous decisions — it runs Python-defined workflows you author, with full visibility into each step. The intelligence is in the workflow you write; The Brain is the runtime that makes it repeatable and observable.

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

## Tech

- Python 3.11+
- PostgreSQL (single state engine across the ecosystem)
- Docker / Docker Compose for deployment
- Integrates with Memory Vault via its REST API

## License

MIT — see [LICENSE](LICENSE).
