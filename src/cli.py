"""
The Brain — command-line interface.

    brain migrate   Run database migrations
    brain status    Show database connection + migration status
    brain run       Run a workflow file
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
