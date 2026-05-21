"""
The Brain — command-line interface.

    brain migrate   Run database migrations
    brain status    Show database connection + migration status
    brain run       Run a workflow file
    brain history   List past workflow runs
    brain show      Show full detail of one run
"""

import asyncio
import logging

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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
