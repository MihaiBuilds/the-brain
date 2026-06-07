"""
Shared pytest fixtures.

Tests use a dedicated ``the_brain_test`` database inside the same Postgres
instance. It is created and migrated once at session start, and dropped at
session end, so the main ``the_brain`` database is never touched.

``workflow_runs`` is truncated before every test so each starts clean
without paying the cost of re-running migrations.

Environment variables are set *before* any ``src.*`` module is imported so
``src.config.settings`` picks up the test database.

The async connection pool is opened and closed *per test* (``_db_pool``).
Each async test runs in its own event loop, and a psycopg pool is bound to
the loop it was opened on — a per-test pool keeps every test self-contained
and lets the synchronous CLI tests run their own ``asyncio.run`` loops
without colliding with a shared pool.
"""

from __future__ import annotations

import os

# Must be set before importing anything from src.* — Settings is a frozen
# dataclass that reads os.environ at import time.
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5433")
os.environ["DB_NAME"] = "the_brain_test"
os.environ.setdefault("DB_USER", "the_brain")
os.environ.setdefault("DB_PASSWORD", "the_brain")

from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

ADMIN_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/postgres"
)
TEST_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
TEST_DB_NAME = os.environ["DB_NAME"]
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ---------------------------------------------------------------------------
# Test database lifecycle — synchronous, runs once per session
# ---------------------------------------------------------------------------


def _terminate_connections(cur) -> None:
    cur.execute(
        """SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()""",
        (TEST_DB_NAME,),
    )


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create the test DB and run migrations once; drop it at session end."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn, conn.cursor() as cur:
        _terminate_connections(cur)
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')

    _run_migrations()

    yield

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn, conn.cursor() as cur:
        _terminate_connections(cur)
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')


def _run_migrations() -> None:
    """Apply every SQL migration file, in order, to the test database."""
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(migration.read_text())


@pytest.fixture(autouse=True)
def _clean_tables(_test_database):
    """Truncate per-test tables before every test (synchronous).

    CASCADE handles the self-FK on ``workflow_runs.previous_run_id`` and
    the FK from ``workflow_schedules.last_run_id``.
    """
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE workflow_runs, workflow_schedules, daemon_heartbeats, "
            "webhook_secrets, file_watchers RESTART IDENTITY CASCADE"
        )
    yield


# ---------------------------------------------------------------------------
# Async connection pool — opened per async test, on that test's own loop
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def db_pool():
    """Open ``src.db``'s pool for one async test and close it afterwards.

    Async tests that touch the database (runner tests) request this
    fixture. CLI tests are synchronous and manage their own pool inside
    each command's ``asyncio.run``, so they do not use it.
    """
    from src.db import close_pool, init_pool

    await init_pool(min_size=1, max_size=5)
    yield
    await close_pool()


# ---------------------------------------------------------------------------
# Clock freezing — for tests that need a deterministic wall clock
# ---------------------------------------------------------------------------


@pytest.fixture
def freeze_clock():
    """Freeze the wall clock at a caller-supplied moment for the test's duration.

    Most scheduler tests don't need this — ``daemon_tick`` takes ``now`` as
    a parameter, so they pin time at the call site. The fixture exists for
    tests that need to mock ``datetime.now()`` itself (e.g. asserting code
    paths that call it directly, like ``run_daemon``'s loop).

    Usage::

        def test_something(freeze_clock):
            with freeze_clock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)):
                ...
    """
    from freezegun import freeze_time

    return freeze_time
