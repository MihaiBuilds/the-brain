# Security Policy

The Brain is a self-hosted workflow orchestrator. Vulnerabilities reported responsibly will be acknowledged, fixed, and credited.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security reports.** Public disclosure before a fix is available puts every Brain user at risk.

Instead, email **support@mihaibuilds.com** with the subject line:

```
Security: <one-line summary>
```

Include:

- A description of the vulnerability and its impact
- Steps to reproduce (or a proof-of-concept)
- Affected version(s) — output of `brain --version` or the Docker image tag
- Your contact info if you'd like credit when the fix lands

Encrypted reports are welcome — request a PGP key in your first email if you want one.

## Response

- **Acknowledgement:** within 7 days
- **Initial assessment:** within 14 days (severity, affected versions, fix plan)
- **Fix + disclosure:** coordinated. Patch lands first; a public advisory with credit follows after users have had a reasonable window to update.

If a report goes 14 days without an acknowledgement, escalate by opening a public issue with the words "security follow-up — no response on private channel" — but do **not** include vulnerability details in that public issue.

## Supported Versions

The Brain is a single-maintainer project. Only the **latest minor release** in the current major series receives security fixes. Older minors will not be backported.

| Version | Supported       |
| ------- | --------------- |
| 1.x     | ✅ Latest minor  |
| < 1.0   | ❌ Pre-release   |

When v2.0 ships, v1.x will receive security fixes for at least 90 days after the v2.0 release.

## Disclosure Policy

The Brain follows **coordinated disclosure**:

1. Fix is developed and tested privately.
2. Patch is released as a tagged version (e.g. `v1.0.1`).
3. A GitHub Security Advisory is published, crediting the reporter (unless anonymity was requested).
4. Users are encouraged to update via `docker compose pull && docker compose up -d`.

Reporters are credited by name and link unless they ask not to be. Bounties are not offered (single-maintainer project, no budget) — the credit and the fix are the reward.

## Out of Scope

- Vulnerabilities in dependencies that have not yet been published as advisories. Please report those upstream first.
- Self-inflicted misconfiguration (e.g. exposing the HTTP API to the public internet without TLS — this is documented as operator-responsibility).
- Social engineering, denial-of-service via raw resource exhaustion (The Brain is designed for self-hosted single-tenant use).
- Issues that require physical or admin access to the host machine.
- **Workflow files themselves.** Workflows are trusted Python imported by `WorkflowLoader`. A workflow file is treated like any other file in your project — if you can write to it, you can already execute arbitrary code on the host. The Brain is not a sandbox.

## Threat Model (v1.0)

The Brain is a **single-tenant, self-hosted** workflow orchestrator. The threat model reflects that posture — it is not an internet-exposed multi-user SaaS, and the security boundary stops at the host's network.

### Who's the user?

A developer or hobbyist running The Brain on their own machine, homelab, or single-purpose VPS. They control the host, the network, the workflow files, and who has access to the API token and webhook secrets. They're security-aware enough to put it behind a reverse proxy with TLS if exposed beyond localhost.

### Data sensitivity

Workflow definitions are code the operator wrote. Run history (step output, errors, durations) is sensitive to the operator but not regulated data. Bearer tokens, webhook HMAC secrets, and LLM API keys are the only secret material handled by the application; database credentials are configured via environment, not stored.

### In-scope attacks

| Attack | Defense |
|---|---|
| Network MITM between client and HTTP API | TLS is the operator's responsibility (reverse proxy in production); the API bearer token is useless without the matching value in the configured env var |
| Webhook forgery | Every webhook trigger requires an HMAC signature header; verification uses `hmac.compare_digest` for constant-time comparison; secrets are 32 random bytes from `secrets.token_urlsafe` (~256 bits of entropy) |
| Stolen API bearer token | Rotate by changing `THE_BRAIN_API_TOKEN` and restarting; the API is single-token by design (no per-user tokens in v1.0) |
| SQL injection via workflow names, step output, search filters | All raw SQL uses `%s` parameterization (no f-string substitution of user values); `brain show` rejects SQL LIKE metacharacters (`%`, `_`) before they reach the query |
| DoS via oversized inputs | Step output captured per run is bounded by Python's normal subprocess pipe buffering; webhook bodies are read once and discarded after dispatch |
| Stack-trace leakage in error responses | The HTTP API returns generic error JSON on failure; full traces go to structured logs only, correlated by `run_id` |
| Credentials leaking via the diagnostic bundle | `brain diagnose` uses an explicit env-var allow-list; `DB_PASSWORD`, `LLM_API_KEY`, `MEMORY_VAULT_TOKEN`, and `THE_BRAIN_API_TOKEN` are recorded as presence-only (name only, no value) and never written to the bundle |

### Out of scope (acknowledged)

| Threat | Why deferred |
|---|---|
| Multi-tenant isolation | The Brain v1.0 is single-user. PRO introduces multi-user with workspace isolation |
| Workflow sandboxing | Workflows are trusted Python by design — sandboxing arbitrary Python is an unsolved problem and not the goal of v1.0 |
| Encryption at rest | Operator's responsibility — use full-disk encryption on the host, or a managed Postgres with TDE |
| External penetration audit | Single-maintainer pre-revenue product; revisit post-launch when there's budget |
| Per-user API tokens | The single `THE_BRAIN_API_TOKEN` env var is the v1.0 model. Multi-token rotation lands in PRO |
| Compromised host machine | Out of scope by design — the host's OS/admin is the trust boundary |
| Docker socket access from the container | The Brain container does **not** mount `/var/run/docker.sock`. The Docker step is intentionally absent from v1.0 |

## Trust Posture for Workflow Inputs

Workflow files are **trusted Python authored by the operator**, but the *inputs* that flow into a workflow at runtime — webhook bodies, file-change events, scheduler timestamps — are **not** automatically trusted. The substitution model (`{previous.X}`, `{trigger.X}`) inserts those values into step fields **textually**, which means a `ShellStep.command` that interpolates a webhook payload is a textbook command-injection surface:

```python
# DANGEROUS — webhook body flows verbatim into /bin/sh
ShellStep(name="echo", command="echo {trigger.message}")
```

A malicious caller can POST `{"message": "x; rm -rf $HOME"}` and execute arbitrary shell. The Brain does **not** auto-escape interpolated values, because workflow authors sometimes legitimately want shell metacharacters in their commands.

### Safe patterns

- **Pass untrusted input through arguments, not the command string.** Use a fixed `ShellStep.command` that reads from an environment variable populated by a prior step, or pass values as separate `argv` elements via a wrapper script. Same advice as `subprocess.run(..., shell=False)` in plain Python.
- **For `McpToolStep`**, interpolated values land in the `args` dict and are passed as MCP tool arguments — they do not pass through a shell. Substitution into `McpToolStep.args` is the recommended path for untrusted external data.
- **For `LLMStep` and `MemoryVaultStep`**, interpolated values become part of the prompt or the JSON body — no shell execution surface. Treat them like any other untrusted string in an LLM/HTTP context (prompt-injection caveats apply for `LLMStep`).

### What The Brain enforces

- Webhook triggers require an HMAC signature header verified with `hmac.compare_digest` against a secret minted via `brain register-webhook <workflow>`. Forged webhooks cannot reach the runner.
- The HTTP API requires the `THE_BRAIN_API_TOKEN` bearer token on every endpoint except `/health`, verified with `secrets.compare_digest`. Unset → the server refuses to start with a clear error.
- `brain show` rejects SQL `LIKE` metacharacters (`%`, `_`) in the run-ID prefix so wildcard queries can't return runs the user did not ask for.
- All raw SQL uses `%s` parameterization; user values never interpolate into SQL strings (verified at v1.0 audit with `bandit -r src/`).
- The diagnostic bundle uses an explicit env-var allow-list; `DB_PASSWORD`, `LLM_API_KEY`, `MEMORY_VAULT_TOKEN`, and `THE_BRAIN_API_TOKEN` are recorded as presence-only and never written by value.

### What's the operator's job

- **TLS for non-localhost exposure.** The HTTP API binds `0.0.0.0` by default so it works inside a Docker container; if you expose it beyond localhost, put a reverse proxy with TLS in front. The bearer token is useless over cleartext HTTP on an untrusted network.
- **Workflow file hygiene.** A malicious or compromised workflow file already runs arbitrary Python on the host — review workflows from third parties the same way you would review any code before running it.
- **Webhook secret rotation.** `brain register-webhook` mints a new secret; revoke old ones when rotating. The Brain stores SHA-256 hashes only — the plaintext is shown once at registration.

## Static Analysis & Dependency Health

Public-tier security tooling enabled in CI:

- **Bandit** (Python) — runs locally before each release; `# nosec` annotations are in-source with justifications.
- **CodeQL** ([.github/workflows/codeql.yml](.github/workflows/codeql.yml)) — security-extended query pack, scans Python on push, PR, and weekly cron.
- **Dependabot** ([.github/dependabot.yml](.github/dependabot.yml)) — weekly checks on Python, GitHub Actions, and Docker base images. Minor and patch updates grouped to reduce PR noise.
