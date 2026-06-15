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

## Static Analysis & Dependency Health

Public-tier security tooling enabled in CI:

- **Bandit** (Python) — runs locally before each release; `# nosec` annotations are in-source with justifications.
- **CodeQL** ([.github/workflows/codeql.yml](.github/workflows/codeql.yml)) — security-extended query pack, scans Python on push, PR, and weekly cron.
- **Dependabot** ([.github/dependabot.yml](.github/dependabot.yml)) — weekly checks on Python, GitHub Actions, and Docker base images. Minor and patch updates grouped to reduce PR noise.
