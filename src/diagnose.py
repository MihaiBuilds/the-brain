"""Bundle a redacted snapshot of a Brain install for bug reports.

``brain diagnose`` runs this module's :func:`build_bundle` to produce a
zip containing the environment, version, status, and logs needed to
triage an issue without forcing the user to hunt for log paths or
copy/paste shell output.

Two privacy properties are deliberate:

1. **Env vars are filtered by an allow-list.** Only known-safe names are
   captured. Secrets (``DB_PASSWORD``, ``LLM_API_KEY``, the value of
   ``THE_BRAIN_API_TOKEN``) are never written to the bundle. The bundle
   contains a top-level ``REDACTED_FIELDS.txt`` listing every env var
   intentionally omitted so the bug-reporter can tell what is missing
   on purpose vs. what is simply unset.
2. **Logs go in as-is.** If the user's own workflow has accidentally
   logged a secret to stdout, the bundle will carry it. The README
   troubleshooting note tells the user to review the bundle before
   posting it to a public issue tracker — defense in depth.

Both Docker and No-Docker installs are supported. The bundle includes a
``MODE`` marker (``docker`` or ``no-docker``) so the bug-reporter
context is obvious without grepping. When ``docker`` isn't available on
the user's ``PATH``, the docker-specific files are skipped with a clear
note instead of failing the command.
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# Env vars we ARE allowed to capture (non-secret configuration).
_ENV_ALLOWLIST: tuple[str, ...] = (
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_NAME",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "MEMORY_VAULT_URL",
)

# Env vars whose VALUE must never appear in the bundle. We record only
# whether they are SET or UNSET — useful triage signal ("the user has
# no DB password configured") without leaking the secret itself.
_ENV_PRESENCE_ONLY: tuple[str, ...] = (
    "DB_PASSWORD",
    "LLM_API_KEY",
    "MEMORY_VAULT_TOKEN",
    "THE_BRAIN_API_TOKEN",
)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run_capture(cmd: list[str], timeout: float = 10.0) -> str:
    """Run a shell command and return its combined output.

    Returns the command's stdout+stderr text on success. On any failure
    (non-zero exit, timeout, missing binary), returns a one-line error
    description prefixed with ``(command failed: ...)`` so the bundle
    captures the failure mode instead of crashing the diagnose pass.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell=True
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return f"(command failed: {cmd[0]} not found on PATH)"
    except subprocess.TimeoutExpired:
        return f"(command failed: timed out after {timeout}s)"
    output = result.stdout
    if result.stderr:
        output = f"{output}\n--- stderr ---\n{result.stderr}"
    if result.returncode != 0:
        output = f"(exit code {result.returncode})\n{output}"
    return output


def _brain_status_output() -> str:
    """Run ``brain status`` in-process and capture its stdout.

    Avoids a re-fork of the CLI; just calls the same async helper the
    command uses. Output matches what the user would see at the terminal.
    """
    # Imported lazily so this module doesn't pull the CLI / DB layer at
    # import time. ``brain diagnose`` is the only consumer.
    import asyncio

    from src import cli as cli_module

    buf = io.StringIO()

    # ``_status()`` uses ``click.echo`` which writes to stdout. Redirect
    # stdout for the duration of the call so the captured text is
    # exactly what the user would see.
    import contextlib

    with contextlib.redirect_stdout(buf):
        try:
            asyncio.run(cli_module._status())
        except Exception as exc:  # pragma: no cover — defensive
            buf.write(f"\n(brain status raised: {exc!r})")

    return buf.getvalue()


def _env_snapshot() -> tuple[dict[str, str], list[str]]:
    """Return ``(captured_values, redacted_fields)``.

    ``captured_values`` maps every allow-listed env var present in the
    environment to its actual value. ``redacted_fields`` is the names
    of presence-only env vars that ARE set — captured by name only,
    never by value.
    """
    captured: dict[str, str] = {}
    for name in _ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value is not None:
            captured[name] = value

    redacted: list[str] = []
    for name in _ENV_PRESENCE_ONLY:
        if os.environ.get(name):
            redacted.append(name)
    return captured, redacted


def _format_env_section(captured: dict[str, str], redacted: list[str]) -> str:
    lines = ["# Brain environment (filtered, non-secret only)\n"]
    if captured:
        for name in sorted(captured):
            lines.append(f"{name}={captured[name]}")
    else:
        lines.append("(no allow-listed env vars present)")

    lines.append("")
    lines.append("# Secrets PRESENT but REDACTED (value never written to bundle)")
    if redacted:
        for name in sorted(redacted):
            lines.append(f"{name}=<redacted: set, value omitted>")
    else:
        lines.append("(no secret env vars set)")
    return "\n".join(lines) + "\n"


def _format_redacted_fields_doc() -> str:
    """Top-level explainer listing what the bundle deliberately omits."""
    redaction_lines = "\n".join(f"- {name}" for name in _ENV_PRESENCE_ONLY)
    return (
        "# Redacted fields\n"
        "\n"
        "The following environment variables are NEVER written to a brain\n"
        "diagnostic bundle, by design. If you see one of these listed in\n"
        "`environment.txt` as `<redacted: set, value omitted>`, that means\n"
        "the variable is configured on the machine but its value has been\n"
        "filtered out.\n"
        "\n"
        f"{redaction_lines}\n"
        "\n"
        "If the variable is not in `environment.txt` at all, it is unset.\n"
        "\n"
        "Logs are included unfiltered. If you (or a workflow you ran) have\n"
        "logged a secret value to stdout, that string will be in the bundle.\n"
        "Review every file before posting the bundle to a public issue\n"
        "tracker.\n"
    )


def _os_info() -> str:
    info = platform.uname()
    return (
        f"system={info.system}\n"
        f"release={info.release}\n"
        f"version={info.version}\n"
        f"machine={info.machine}\n"
        f"processor={info.processor}\n"
        f"python={sys.version}\n"
    )


def _brain_version() -> str:
    try:
        return _pkg_version("the-brain")
    except PackageNotFoundError:
        return "(package not installed via pip — version unknown)"


def build_bundle(target_dir: Path | None = None, now: datetime | None = None) -> Path:
    """Build a diagnostic bundle and return the path to the zip file.

    Args:
        target_dir: directory the zip is written into. Defaults to the
            current working directory.
        now: timestamp used in the bundle filename. Tests pin this for
            deterministic naming.

    Returns:
        The path to the newly created zip.
    """
    target = target_dir if target_dir is not None else Path.cwd()
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d-%H%M%S")
    zip_path = target / f"brain-diagnostic-{stamp}.zip"

    docker_mode = _docker_available()
    captured_env, redacted_env = _env_snapshot()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Mode marker — first thing a triager looks at.
        zf.writestr("MODE.txt", f"MODE: {'docker' if docker_mode else 'no-docker'}\n")

        # Redaction explainer — second thing a triager looks at.
        zf.writestr("REDACTED_FIELDS.txt", _format_redacted_fields_doc())

        # Filtered env snapshot.
        zf.writestr("environment.txt", _format_env_section(captured_env, redacted_env))

        # OS + Python info.
        zf.writestr("os_info.txt", _os_info())

        # Brain version.
        zf.writestr("brain_version.txt", _brain_version() + "\n")

        # `brain status` — same output the user would see at the terminal.
        zf.writestr("brain_status.txt", _brain_status_output())

        if docker_mode:
            zf.writestr(
                "docker_compose_ps.txt",
                _run_capture(["docker", "compose", "ps"]),
            )
            zf.writestr(
                "docker_logs_brain.txt",
                _run_capture(["docker", "compose", "logs", "brain", "--tail=1000"]),
            )
            zf.writestr(
                "docker_logs_db.txt",
                _run_capture(["docker", "compose", "logs", "db", "--tail=500"]),
            )
        else:
            zf.writestr(
                "docker_skipped.txt",
                "Docker not detected on PATH — docker compose ps / logs skipped.\n"
                "If The Brain is running under Docker on this host, rerun\n"
                "`brain diagnose` from a shell where the docker CLI is available.\n",
            )

    return zip_path
