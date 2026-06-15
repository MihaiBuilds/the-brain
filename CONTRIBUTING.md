# Contributing to The Brain

Thanks for your interest in The Brain. This is a single-maintainer open-source project, so contributions are welcome but reviewed when time allows. Please read this guide before opening an issue or PR — it saves both of us time.

By participating in this project you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Table of Contents

- [Reporting Bugs](#reporting-bugs)
- [Suggesting Features](#suggesting-features)
- [Reporting Security Vulnerabilities](#reporting-security-vulnerabilities)
- [Asking Questions](#asking-questions)
- [Setting Up a Dev Environment](#setting-up-a-dev-environment)
- [Coding Conventions](#coding-conventions)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [What Gets Merged](#what-gets-merged)

---

## Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml) — it walks you through the information that's actually useful.

**Before opening an issue, run the diagnostic bundler:**

```bash
docker compose exec brain brain diagnose
```

(Or `brain diagnose` directly if you're running on the host.)

This produces a `brain-diagnostic-YYYY-MM-DD-HHMMSS.zip` containing:

- Recent application logs (when run under Docker)
- Output of `brain status` and `brain --version`
- Platform / Python / environment info — only an allow-listed set of env vars is recorded with values; `DB_PASSWORD`, `LLM_API_KEY`, `MEMORY_VAULT_TOKEN`, and `THE_BRAIN_API_TOKEN` are recorded as presence-only (name appears, value does not)
- `docker compose ps` and recent DB logs (when run on the host alongside `docker compose`)

**Review the zip before posting it.** Redaction is a safety net, not a guarantee — if your workflow logged anything sensitive (a step that echoed a secret, an LLM prompt that included a key), scrub it first.

Every workflow run has a `run_id` that appears in the structured logs. Quoting that ID in the issue helps me find the exact log lines fast.

## Suggesting Features

Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.yml). Two things make a feature request likely to land:

1. **Frame the problem first, then propose a solution.** "I can't do X because Y" beats "please add Z."
2. **Check the [Limitations section in the README](README.md#limitations).** If it overlaps with a documented v1.0 limitation, a +1 on the existing issue (or opening one) is more useful than a duplicate request.

Big features (new step types, new trigger types, new integrations) should be discussed in an issue *before* any code is written. Drive-by PRs for big features will likely be closed politely.

## Reporting Security Vulnerabilities

**Do not open a public issue for security reports.** See [SECURITY.md](SECURITY.md) for the private disclosure process.

## Asking Questions

GitHub Issues is for bugs and concrete feature requests. For setup help, design questions, or "how do I…" — use [GitHub Discussions](https://github.com/MihaiBuilds/the-brain/discussions). Questions opened as issues will be moved to Discussions.

---

## Setting Up a Dev Environment

The Brain runs in Docker for production, but local development uses a Python venv against a Dockerized Postgres.

### Prerequisites

- Python 3.11+
- Docker + Docker Compose
- (Optional) LM Studio for exercising `LLMStep` end-to-end

### Backend setup

```bash
git clone https://github.com/MihaiBuilds/the-brain.git
cd the-brain

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start Postgres only (The Brain runs locally for fast iteration)
docker compose up -d db

brain migrate
brain status   # sanity check

# Run a workflow
brain run examples/hello.py
```

### Running tests

```bash
pytest                                  # full suite
pytest tests/test_cli.py -v            # one file
pytest -k "diagnose"                   # by keyword
```

The test suite expects the Dockerized Postgres from `docker compose up -d db` to be reachable on `localhost:5432` with the env vars from `.env.example`.

---

## Coding Conventions

The Brain is small enough that consistency matters more than rules. The general shape:

**Python**

- Python 3.11+, type hints on public functions
- Raw SQL via `psycopg`, no ORM
- `async`/`await` in the runner / API / scheduler; sync is fine for CLI commands
- Errors at the HTTP boundary return structured JSON; the CLI prints a single line and exits non-zero
- Log identifiers (`run_id`, `workflow_name`, step name), never raw step output or LLM prompts
- Workflow files are user-authored Python; their imports load through `WorkflowLoader` — do not add an `eval`/`exec` path for them

**Commits**

- Imperative mood ("add diagnose command", not "added diagnose command")
- One logical change per commit when reasonable
- No `Co-Authored-By` lines

**What to avoid**

- Adding new dependencies without justification — the dependency tree is deliberately small
- Speculative abstractions or "future flexibility" — match the existing direct style
- Comments that restate the code; comments explaining *why* are welcome
- Logging user-supplied content (workflow step output, LLM prompts, webhook bodies) — only IDs and metadata

---

## Submitting a Pull Request

1. **Open an issue first** for anything beyond a small fix. Saves wasted work if the direction isn't right.
2. **Fork → branch off `main`** with a descriptive name (`fix/scheduler-skew`, `feat/http-step`).
3. **Keep PRs focused.** One concern per PR. Refactoring + feature in the same PR usually gets split.
4. **Run tests + lint locally** before pushing:
   ```bash
   pytest
   ruff check .
   ```
5. **Update docs** when you change user-facing behavior — README, docstrings, or the FAQ.
6. **Fill in the PR template** — it asks for the things I'd otherwise have to ask for in review.

PRs that don't pass CI will not be reviewed until they do. PRs that grow the dependency footprint significantly will get pushback.

## What Gets Merged

**Likely merged:**

- Bug fixes with a test that fails before and passes after
- Documentation improvements (typos, clarifications, missing examples)
- Performance improvements with measurements
- Small UX improvements to the CLI (clearer error messages, better `--help` text)
- Additional examples under `examples/`

**Likely deferred or declined:**

- Features that belong in the PRO tier (multi-user, hosted scheduler, conflict resolution, etc.) — these have a planned home
- New step types that add ongoing maintenance burden without a clear use case
- New trigger types that require always-on external services
- Large refactors of working code without a concrete payoff
- Style-only changes that fight the existing conventions

If you're unsure whether something fits, open an issue and ask before writing the code.

---

Thanks for reading this far. If you ship something to The Brain, you'll be credited in the release notes.

— Mihai
