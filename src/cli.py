"""
The Brain — command-line interface.

    brain migrate     Run database migrations
    brain status      Show database connection + migration status
    brain run         Run a workflow file
    brain history     List past workflow runs
    brain show        Show full detail of one run
    brain register    Register a workflow on a cron schedule
    brain list        List registered schedules
    brain disable     Soft-disable a schedule (the daemon will skip it)
    brain enable      Re-enable a schedule
    brain unregister  Hard-delete a schedule
    brain daemon      Run the scheduler daemon (long-running)
    brain daemon-status  Check whether the daemon is healthy (exit 0 if yes)
    brain serve       Run the HTTP API (long-running, requires THE_BRAIN_API_TOKEN)
    brain register-webhook    Register a workflow as a webhook trigger
    brain disable-webhook     Soft-disable a webhook (it will return 404)
    brain enable-webhook      Re-enable a webhook
    brain unregister-webhook  Hard-delete a webhook registration
    brain list-triggers       Unified view of cron schedules + webhooks + watchers
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import click

from src.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@click.group()
def cli() -> None:
    """The Brain — workflow orchestrator for the MihaiBuilds ecosystem."""


@cli.command()
def migrate() -> None:
    """Run database migrations."""
    asyncio.run(_migrate())


async def _migrate() -> None:
    from src.db import close_pool, init_pool, run_migrations

    await init_pool()
    await run_migrations()
    await close_pool()
    click.echo("Migrations complete.")


@cli.command()
def status() -> None:
    """Show database connection and migration status."""
    asyncio.run(_status())


async def _status() -> None:
    from src.db import close_pool, fetch_all, health_check, init_pool

    await init_pool()
    health = await health_check()
    click.echo(f"Database: {health['status']}")
    if health["status"] == "healthy":
        click.echo(f"  Server: {health['server_version']}")

        table_exists = await fetch_all("SELECT to_regclass('_migrations') AS t")
        if not table_exists or table_exists[0]["t"] is None:
            click.echo("Migrations applied: 0 — run `brain migrate`")
            await close_pool()
            return

        applied = await fetch_all("SELECT filename, applied_at FROM _migrations ORDER BY filename")
        if applied:
            click.echo(f"Migrations applied: {len(applied)}")
            for row in applied:
                click.echo(f"  • {row['filename']} ({row['applied_at']})")
        else:
            click.echo("Migrations applied: 0 — run `brain migrate`")
    else:
        click.echo(f"  Error: {health.get('error', 'unknown')}")

    await close_pool()


@cli.command()
@click.argument("workflow_path", type=click.Path())
def run(workflow_path: str) -> None:
    """Run a workflow file.

    WORKFLOW_PATH is a .py file defining a module-level 'workflow'.
    Exits 0 only if every step succeeds; exits 1 on any failure.
    """
    asyncio.run(_run(workflow_path))


async def _run(workflow_path: str) -> None:
    from src.db import close_pool, init_pool
    from src.executors.base import StepResult
    from src.runner import run_workflow
    from src.workflow.loader import WorkflowLoadError, import_workflow_from_file

    try:
        workflow = import_workflow_from_file(workflow_path)
    except WorkflowLoadError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    def show_step(result: StepResult) -> None:
        if result.success:
            click.echo(f"  ✓ {result.step_name}")
        else:
            click.echo(f"  ✗ {result.step_name}: {result.error}")

    click.echo(f"Running workflow {workflow.name!r} ({len(workflow.steps)} steps)")
    await init_pool()
    try:
        workflow_run = await run_workflow(workflow, workflow_path, on_step_complete=show_step)
    finally:
        await close_pool()

    short_id = str(workflow_run.id)[:8]
    click.echo(f"Run {short_id} — {workflow_run.status}")
    if workflow_run.status != "success":
        raise SystemExit(1)


def _duration(started_at: object, ended_at: object) -> str:
    """Human-readable run duration, or '—' if the run has not ended."""
    if started_at is None or ended_at is None:
        return "—"
    seconds = (ended_at - started_at).total_seconds()  # type: ignore[operator]
    return f"{seconds:.1f}s"


@cli.command()
@click.option("--limit", default=20, show_default=True, help="Max runs to show.")
@click.option("--workflow", "workflow_name", help="Filter by workflow name.")
@click.option(
    "--status",
    type=click.Choice(["running", "success", "failed"]),
    help="Filter by run status.",
)
def history(limit: int, workflow_name: str | None, status: str | None) -> None:
    """List past workflow runs, most recent first."""
    asyncio.run(_history(limit, workflow_name, status))


async def _history(limit: int, workflow_name: str | None, status: str | None) -> None:
    from src.db import close_pool, fetch_all, init_pool

    clauses: list[str] = []
    params: list[object] = []
    if workflow_name:
        clauses.append("workflow_name = %s")
        params.append(workflow_name)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    await init_pool()
    try:
        rows = await fetch_all(
            f"""
            SELECT id, workflow_name, status, started_at, ended_at
            FROM workflow_runs
            {where}
            ORDER BY started_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
    finally:
        await close_pool()

    if not rows:
        click.echo("No runs found.")
        return

    click.echo(f"{'RUN':<10}{'WORKFLOW':<24}{'STATUS':<10}{'STARTED':<22}DURATION")
    for row in rows:
        short_id = str(row["id"])[:8]
        started = row["started_at"].strftime("%Y-%m-%d %H:%M:%S")
        duration = _duration(row["started_at"], row["ended_at"])
        click.echo(
            f"{short_id:<10}{row['workflow_name'][:23]:<24}"
            f"{row['status']:<10}{started:<22}{duration}"
        )


@cli.command()
@click.argument("run_id")
def show(run_id: str) -> None:
    """Show full detail of one run.

    RUN_ID may be a prefix of the run's ID — the short ID from
    `brain run` or `brain history` works.
    """
    asyncio.run(_show(run_id))


async def _show(run_id: str) -> None:
    from src.db import close_pool, fetch_all, init_pool

    await init_pool()
    try:
        matches = await fetch_all(
            "SELECT * FROM workflow_runs WHERE id::text LIKE %s ORDER BY started_at",
            (f"{run_id}%",),
        )
    finally:
        await close_pool()

    if not matches:
        click.echo(f"Error: no run matching {run_id!r}", err=True)
        raise SystemExit(1)
    if len(matches) > 1:
        click.echo(f"Error: {run_id!r} is ambiguous — matches:", err=True)
        for row in matches:
            click.echo(f"  {str(row['id'])[:8]}  {row['workflow_name']}", err=True)
        raise SystemExit(1)

    run = matches[0]
    click.echo(f"Run:      {run['id']}")
    click.echo(f"Workflow: {run['workflow_name']}")
    click.echo(f"File:     {run['workflow_file_path']}")
    click.echo(f"Status:   {run['status']}")
    click.echo(f"Started:  {run['started_at']}")
    click.echo(f"Ended:    {run['ended_at'] or '—'}")
    click.echo(f"Duration: {_duration(run['started_at'], run['ended_at'])}")
    if run["error"]:
        click.echo(f"Error:    {run['error']}")

    output = run["output"] or []
    if output:
        click.echo("\nSteps:")
        for step in output:
            mark = "✓" if step.get("success") else "✗"
            click.echo(f"  {mark} {step.get('name')}")
            step_output = step.get("output", "")
            if step_output:
                for line in step_output.splitlines():
                    click.echo(f"      {line}")


# ---------------------------------------------------------------------------
# Schedule lifecycle commands
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("workflow_path", type=click.Path())
@click.option("--cron", "cron_expr", required=True, help="Standard 5-field cron expression.")
@click.option(
    "--name",
    "name_override",
    help="Register under this name instead of the workflow's own name.",
)
def register(workflow_path: str, cron_expr: str, name_override: str | None) -> None:
    """Register a workflow on a cron schedule.

    WORKFLOW_PATH is a .py file defining a module-level 'workflow'. The cron
    expression is validated and the workflow file is loaded before the
    schedule row is inserted. Duplicate names are rejected.
    """
    asyncio.run(_register(workflow_path, cron_expr, name_override))


async def _register(workflow_path: str, cron_expr: str, name_override: str | None) -> None:
    from src.db import close_pool, execute_query, fetch_one, init_pool
    from src.scheduler import CronExpression, InvalidCronError
    from src.workflow.loader import WorkflowLoadError, import_workflow_from_file

    try:
        cron = CronExpression.parse(cron_expr)
    except InvalidCronError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    try:
        workflow = import_workflow_from_file(workflow_path)
    except WorkflowLoadError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    name = name_override or workflow.name
    absolute_path = str(Path(workflow_path).resolve())
    next_fire = cron.next_fire_after(datetime.now(UTC))

    await init_pool()
    try:
        existing = await fetch_one(
            "SELECT 1 FROM workflow_schedules WHERE workflow_name = %s",
            (name,),
        )
        if existing is not None:
            click.echo(
                f"Error: a schedule named {name!r} already exists — "
                f"use `brain unregister {name}` first, or pass --name to register under a different name",
                err=True,
            )
            raise SystemExit(1)

        await execute_query(
            """
            INSERT INTO workflow_schedules
                (workflow_name, workflow_file_path, cron_expression, next_run_at)
            VALUES (%s, %s, %s, %s)
            """,
            (name, absolute_path, str(cron), next_fire),
        )
    finally:
        await close_pool()

    click.echo(f"Registered {name!r} — next fire {next_fire.strftime('%Y-%m-%d %H:%M:%S %Z')}")


@cli.command(name="list")
@click.option("--enabled", "filter_enabled", is_flag=True, help="Show only enabled schedules.")
@click.option("--disabled", "filter_disabled", is_flag=True, help="Show only disabled schedules.")
@click.option("--workflow", "workflow_name", help="Show only the schedule with this name.")
def list_cmd(filter_enabled: bool, filter_disabled: bool, workflow_name: str | None) -> None:
    """List registered schedules."""
    if filter_enabled and filter_disabled:
        click.echo("Error: --enabled and --disabled are mutually exclusive", err=True)
        raise SystemExit(1)
    asyncio.run(_list(filter_enabled, filter_disabled, workflow_name))


async def _list(filter_enabled: bool, filter_disabled: bool, workflow_name: str | None) -> None:
    from src.db import close_pool, fetch_all, init_pool

    clauses: list[str] = []
    params: list[object] = []
    if filter_enabled:
        clauses.append("s.enabled = true")
    if filter_disabled:
        clauses.append("s.enabled = false")
    if workflow_name:
        clauses.append("s.workflow_name = %s")
        params.append(workflow_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    await init_pool()
    try:
        rows = await fetch_all(
            f"""
            SELECT s.workflow_name, s.cron_expression, s.enabled,
                   s.workflow_file_path, s.next_run_at,
                   r.started_at AS last_run_at
              FROM workflow_schedules s
         LEFT JOIN workflow_runs r ON r.id = s.last_run_id
            {where}
          ORDER BY s.workflow_name
            """,
            tuple(params),
        )
    finally:
        await close_pool()

    if not rows:
        click.echo("No schedules found.")
        return

    click.echo(f"{'NAME':<20}{'CRON':<16}{'ENABLED':<10}{'LAST RUN':<22}{'NEXT FIRE':<22}FILE")
    for row in rows:
        last_run = row["last_run_at"].strftime("%Y-%m-%d %H:%M:%S") if row["last_run_at"] else "—"
        next_fire = row["next_run_at"].strftime("%Y-%m-%d %H:%M:%S") if row["next_run_at"] else "—"
        enabled = "yes" if row["enabled"] else "no"
        click.echo(
            f"{row['workflow_name'][:19]:<20}{row['cron_expression'][:15]:<16}"
            f"{enabled:<10}{last_run:<22}{next_fire:<22}{row['workflow_file_path']}"
        )


@cli.command()
@click.argument("name")
def disable(name: str) -> None:
    """Soft-disable a schedule. The daemon will skip it on the next tick."""
    asyncio.run(_set_enabled(name, False))


@cli.command()
@click.argument("name")
def enable(name: str) -> None:
    """Re-enable a previously disabled schedule."""
    asyncio.run(_set_enabled(name, True))


async def _set_enabled(name: str, enabled: bool) -> None:
    from src.db import close_pool, execute_query, init_pool

    await init_pool()
    try:
        rowcount = await execute_query(
            "UPDATE workflow_schedules SET enabled = %s WHERE workflow_name = %s",
            (enabled, name),
        )
    finally:
        await close_pool()

    if rowcount == 0:
        click.echo(f"Error: no schedule named {name!r}", err=True)
        raise SystemExit(1)

    verb = "enabled" if enabled else "disabled"
    click.echo(f"{name!r} {verb}.")


@cli.command()
@click.argument("name")
def unregister(name: str) -> None:
    """Hard-delete a schedule. Past run rows for this workflow are not affected."""
    asyncio.run(_unregister(name))


async def _unregister(name: str) -> None:
    from src.db import close_pool, execute_query, init_pool

    await init_pool()
    try:
        rowcount = await execute_query(
            "DELETE FROM workflow_schedules WHERE workflow_name = %s",
            (name,),
        )
    finally:
        await close_pool()

    if rowcount == 0:
        click.echo(f"Error: no schedule named {name!r}", err=True)
        raise SystemExit(1)

    click.echo(f"Unregistered {name!r}.")


@cli.command()
def daemon() -> None:
    """Run the scheduler daemon.

    Long-running process. Polls workflow_schedules every 10 seconds, fires
    due workflows sequentially, advances next_run_at after each fire. On
    boot, any workflow_runs row still in 'running' status is recovered as
    a failed run from a previous crash. SIGTERM or SIGINT triggers a
    graceful shutdown after the current workflow finishes.
    """
    asyncio.run(_daemon())


async def _daemon() -> None:
    from src.db import close_pool, init_pool
    from src.scheduler import run_daemon

    await init_pool()
    try:
        await run_daemon()
    finally:
        await close_pool()


HEARTBEAT_STALE_SECONDS = 30


@cli.command(name="daemon-status")
def daemon_status() -> None:
    """Check whether the scheduler daemon is healthy.

    Reads the most recent heartbeat from ``daemon_heartbeats`` and exits 0
    if it was written within the last 30 seconds. Designed for use as a
    Docker healthcheck; also useful interactively.
    """
    asyncio.run(_daemon_status())


async def _daemon_status() -> None:
    from src.db import close_pool, fetch_one, init_pool

    await init_pool()
    try:
        heartbeat = await fetch_one(
            "SELECT daemon_id, last_tick_at FROM daemon_heartbeats "
            "ORDER BY last_tick_at DESC LIMIT 1"
        )
    finally:
        await close_pool()

    if heartbeat is None:
        click.echo("unhealthy: no heartbeat row — daemon has never started")
        raise SystemExit(1)

    age_seconds = (datetime.now(UTC) - heartbeat["last_tick_at"]).total_seconds()
    short_id = heartbeat["daemon_id"][:12]

    if age_seconds > HEARTBEAT_STALE_SECONDS:
        click.echo(
            f"unhealthy: last tick {age_seconds:.0f}s ago "
            f"(threshold {HEARTBEAT_STALE_SECONDS}s, daemon {short_id})"
        )
        raise SystemExit(1)

    click.echo(f"healthy: last tick {age_seconds:.0f}s ago (daemon {short_id})")


@cli.command()
@click.option("--port", default=8001, show_default=True, help="HTTP port to bind.")
@click.option("--host", default="0.0.0.0", show_default=True, help="Interface to bind.")
def serve(port: int, host: str) -> None:
    """Run the HTTP API.

    Long-running process. Exposes ``POST /run`` for executing workflows
    via HTTP. Bearer token is required via the ``THE_BRAIN_API_TOKEN``
    environment variable; the server refuses to start without it.

    This is a separate process from the scheduler daemon — by design.
    Run it in its own container behind the ``api`` compose profile:
    ``docker compose --profile api up -d``.
    """
    import uvicorn

    from src.api import create_app

    try:
        app = create_app()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    uvicorn.run(app, host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# Webhook trigger lifecycle (M3)
# ---------------------------------------------------------------------------


@cli.command(name="register-webhook")
@click.argument("workflow_path", type=click.Path())
@click.option(
    "--name",
    "name_override",
    help="Register under this name instead of the workflow's own name.",
)
def register_webhook(workflow_path: str, name_override: str | None) -> None:
    """Register a workflow as a webhook trigger.

    WORKFLOW_PATH is a .py file defining a module-level 'workflow'. The
    workflow file is loaded before the row is inserted, and a fresh
    HMAC-SHA256 secret is generated and printed to stdout. Save it now:
    it cannot be retrieved later.
    """
    asyncio.run(_register_webhook(workflow_path, name_override))


async def _register_webhook(workflow_path: str, name_override: str | None) -> None:
    from src.db import close_pool, execute_query, fetch_one, init_pool
    from src.triggers.hmac import generate_secret
    from src.workflow.loader import WorkflowLoadError, import_workflow_from_file

    try:
        workflow = import_workflow_from_file(workflow_path)
    except WorkflowLoadError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    name = name_override or workflow.name
    secret = generate_secret()

    await init_pool()
    try:
        existing = await fetch_one(
            "SELECT 1 FROM webhook_secrets WHERE workflow_name = %s",
            (name,),
        )
        if existing is not None:
            click.echo(
                f"Error: a webhook named {name!r} already exists — "
                f"use `brain unregister-webhook {name}` first, "
                "or pass --name to register under a different name",
                err=True,
            )
            raise SystemExit(1)

        await execute_query(
            "INSERT INTO webhook_secrets "
            "(workflow_name, hmac_secret, workflow_file_path) VALUES (%s, %s, %s)",
            (name, secret, str(Path(workflow_path).resolve())),
        )
    finally:
        await close_pool()

    click.echo(f"Registered webhook {name!r}.")
    click.echo("")
    click.echo("Save this secret now — it cannot be retrieved later:")
    click.echo("")
    click.echo(f"  {secret}")
    click.echo("")
    click.echo(
        "Sign the request body with HMAC-SHA256 and send the digest in "
        "the X-Brain-Signature header as `sha256=<hex>`."
    )


@cli.command(name="disable-webhook")
@click.argument("name")
def disable_webhook(name: str) -> None:
    """Soft-disable a webhook. The endpoint will respond 404 until re-enabled."""
    asyncio.run(_set_webhook_enabled(name, False))


@cli.command(name="enable-webhook")
@click.argument("name")
def enable_webhook(name: str) -> None:
    """Re-enable a previously disabled webhook."""
    asyncio.run(_set_webhook_enabled(name, True))


async def _set_webhook_enabled(name: str, enabled: bool) -> None:
    from src.db import close_pool, execute_query, init_pool

    await init_pool()
    try:
        rowcount = await execute_query(
            "UPDATE webhook_secrets SET enabled = %s WHERE workflow_name = %s",
            (enabled, name),
        )
    finally:
        await close_pool()

    if rowcount == 0:
        click.echo(f"Error: no webhook named {name!r}", err=True)
        raise SystemExit(1)

    verb = "enabled" if enabled else "disabled"
    click.echo(f"Webhook {name!r} {verb}.")


@cli.command(name="unregister-webhook")
@click.argument("name")
def unregister_webhook(name: str) -> None:
    """Hard-delete a webhook registration. Past run rows are not affected."""
    asyncio.run(_unregister_webhook(name))


async def _unregister_webhook(name: str) -> None:
    from src.db import close_pool, execute_query, init_pool

    await init_pool()
    try:
        rowcount = await execute_query(
            "DELETE FROM webhook_secrets WHERE workflow_name = %s",
            (name,),
        )
    finally:
        await close_pool()

    if rowcount == 0:
        click.echo(f"Error: no webhook named {name!r}", err=True)
        raise SystemExit(1)

    click.echo(f"Unregistered webhook {name!r}.")


# ---------------------------------------------------------------------------
# Unified trigger listing (M3)
# ---------------------------------------------------------------------------


@cli.command(name="list-triggers")
def list_triggers() -> None:
    """Unified view of all registered triggers — cron, webhook, file.

    Shows one row per registered trigger across the three trigger types,
    with the trigger type, workflow name, and enabled state. File-watcher
    rows appear here as soon as `brain register-watcher` lands; until
    then the watcher rows section is empty.
    """
    asyncio.run(_list_triggers())


async def _list_triggers() -> None:
    from src.db import close_pool, fetch_all, init_pool

    await init_pool()
    try:
        schedules = await fetch_all(
            "SELECT workflow_name, cron_expression, enabled "
            "FROM workflow_schedules ORDER BY workflow_name"
        )
        webhooks = await fetch_all(
            "SELECT workflow_name, enabled FROM webhook_secrets ORDER BY workflow_name"
        )
        watchers = await fetch_all(
            "SELECT workflow_name, watched_path, enabled FROM file_watchers ORDER BY workflow_name"
        )
    finally:
        await close_pool()

    if not schedules and not webhooks and not watchers:
        click.echo("No triggers registered.")
        return

    click.echo(f"{'TYPE':<10}{'NAME':<24}{'ENABLED':<10}DETAIL")
    for row in schedules:
        enabled = "yes" if row["enabled"] else "no"
        click.echo(
            f"{'cron':<10}{row['workflow_name'][:23]:<24}{enabled:<10}{row['cron_expression']}"
        )
    for row in webhooks:
        enabled = "yes" if row["enabled"] else "no"
        click.echo(f"{'webhook':<10}{row['workflow_name'][:23]:<24}{enabled:<10}—")
    for row in watchers:
        enabled = "yes" if row["enabled"] else "no"
        click.echo(f"{'file':<10}{row['workflow_name'][:23]:<24}{enabled:<10}{row['watched_path']}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
