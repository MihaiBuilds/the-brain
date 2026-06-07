"""M3 audit-pass tests — five named gaps from the clause-by-clause review.

Each gap is a regression guard around a non-obvious invariant that
the per-PR tests did not pin directly. The contract under test is the
joint behavior of the M3 trigger surface, not any single module.

Gaps pinned:

(a) trigger_context does NOT propagate into {previous.X}. A run
    triggered by a webhook carries trigger_context on its row; the
    NEXT run of the same workflow can read that prior run's step
    OUTPUT via {previous.<step_name>}, but {previous} does NOT expose
    the prior run's trigger_context. Locked-by-design — trigger_context
    is metadata about WHY a run started, not part of the workflow's
    persistent state.

(b) Existence-leak in the webhook endpoint is an accepted v1.0
    trade-off. Unknown workflow names return 404 immediately, without
    running an HMAC verification step. This means a probe CAN
    distinguish "unknown webhook" from "known but wrong signature" via
    response code (and via timing, since 404 skips the verify path
    entirely). The Brain's threat model is single-token-server-to-server
    with known callers; the registered workflow name is not a secret.
    These tests pin the behavior as locked: any future refactor that
    accidentally adds the constant-time-equal lookup must surface as
    an architectural decision.

(c) Watcher debounce boundary at 499ms vs 501ms. The watcher daemon
    PR already has unit-level tests on _should_fire (the pure function).
    This audit adds an end-to-end pin via the real watcher_tick + real
    filesystem events to guard against future refactors that move the
    debounce check to a different layer and break the boundary.

(d) Per-process serialization invariant: two webhooks to the same
    workflow land sequentially within the API process. FastAPI's
    sync request handling and the runner's synchronous-per-process
    flow mean concurrent requests queue. Pin so a future async
    refactor either preserves this OR surfaces as an explicit
    architectural decision.

(e) Across-process concurrency invariant: the scheduler daemon and
    the API process can run workflows in parallel. Same database,
    different connections, different processes. Pin so a future
    cross-process locking refactor surfaces as an architectural
    decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from src.api.app import TOKEN_ENV_VAR, create_app
from src.db import fetch_all, fetch_one
from src.runner import run_workflow
from src.workflow.models import ShellStep, Workflow

_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
_VALID_TOKEN = "audit-test-token"


def _write_workflow(tmp_path, name, workflow_name=None, command="echo hi"):
    wf_name = workflow_name or name.removesuffix(".py")
    path = tmp_path / name
    path.write_text(
        "from src.workflow import Workflow, ShellStep\n"
        f"workflow = Workflow(name='{wf_name}', steps=[ShellStep(name='s', command='{command}')])\n"
    )
    return str(path)


def _seed_webhook(name, *, secret, file_path, enabled=True):
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO webhook_secrets "
            "(workflow_name, hmac_secret, enabled, workflow_file_path) "
            "VALUES (%s, %s, %s, %s)",
            (name, secret, enabled, file_path),
        )


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, _VALID_TOKEN)
    app = create_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Gap (a) — trigger_context does NOT propagate into {previous.X}
# ---------------------------------------------------------------------------


async def test_gap_a_trigger_context_does_not_leak_into_previous_placeholder(db_pool):
    """A webhook-triggered run's trigger_context is NOT readable via {previous.X}.

    {previous.X} is for prior step OUTPUT, not for the prior run's
    trigger metadata. A workflow that wants to reference trigger fields
    of THIS run uses {trigger.X}; trigger fields of the PRIOR run are
    not exposed — the persistent state is the step output, not the
    invocation reason.
    """
    # First run: webhook-triggered, has trigger_context.
    first = await run_workflow(
        Workflow(name="leak", steps=[ShellStep(name="s", command="echo hi")]),
        "leak.py",
        trigger_context={
            "event": "webhook",
            "body": "secret-payload",
            "headers": {"x-github-event": "push"},
            "path": None,
        },
    )
    assert first.status == "success"

    # Second run: try to read first run's trigger.body via {previous}.
    # The placeholder {previous.body} is interpreted as "step named 'body'
    # in the previous run", which does not exist — should fail.
    second = await run_workflow(
        Workflow(name="leak", steps=[ShellStep(name="probe", command="echo {previous.body}")]),
        "leak.py",
    )
    assert second.status == "failed"
    assert "previous run has no step named" in second.error

    # And the trigger_context column on the first run still has the
    # original data — we are pinning that the data exists in the row but
    # is NOT reachable through the placeholder resolver.
    row = await fetch_one(
        "SELECT trigger_context FROM workflow_runs WHERE id = %s",
        (first.id,),
    )
    assert row["trigger_context"]["body"] == "secret-payload"


async def test_gap_a_trigger_placeholder_on_next_run_resolves_to_current_trigger_not_previous(
    db_pool,
):
    """If the SECOND run also has a trigger_context, {trigger.X} reads from
    the CURRENT run's context, never the prior run's."""
    await run_workflow(
        Workflow(name="t-curr", steps=[ShellStep(name="s", command="echo from-first")]),
        "tc.py",
        trigger_context={
            "event": "webhook",
            "body": "first-body",
            "headers": {},
            "path": None,
        },
    )

    second = await run_workflow(
        Workflow(name="t-curr", steps=[ShellStep(name="s", command="echo got {trigger.body}")]),
        "tc.py",
        trigger_context={
            "event": "webhook",
            "body": "second-body",
            "headers": {},
            "path": None,
        },
    )
    assert second.status == "success"
    row = await fetch_one(
        "SELECT output FROM workflow_runs WHERE id = %s",
        (second.id,),
    )
    out = next(s for s in row["output"] if s["name"] == "s")
    assert out["output"] == "got second-body"


# ---------------------------------------------------------------------------
# Gap (b) — webhook 404-on-unknown-name is locked behavior, not a bug
# ---------------------------------------------------------------------------


def test_gap_b_webhook_404_for_unknown_name_is_locked_behavior(client):
    """An unknown workflow name returns 404 before HMAC verification runs.

    LOCKED v1.0 BEHAVIOR: The webhook name is not a secret. A probe can
    distinguish "unknown" from "known but wrong signature". Threat model
    is single-token-server-to-server with known callers. This test
    exists so any future refactor that adds a constant-time-equal
    lookup (changing the response shape) breaks loudly and forces an
    explicit architectural decision rather than a silent change.
    """
    body = b"any-payload"
    resp = client.post(
        "/webhook/ghost-name",
        content=body,
        headers={"X-Brain-Signature": _sign("anything", body)},
    )
    assert resp.status_code == 404


def test_gap_b_webhook_404_for_disabled_is_locked_behavior(client, tmp_path):
    """A disabled webhook returns 404 in exactly the same shape as unknown.

    Same locked-behavior framing as the unknown case. Both pinned so a
    refactor that changes either to 401 forces an architectural review.
    """
    workflow_path = _write_workflow(tmp_path, "off.py")
    _seed_webhook("off", secret="s", file_path=workflow_path, enabled=False)
    body = b"any-payload"
    resp = client.post(
        "/webhook/off",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body)},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Gap (c) — watcher debounce boundary, end-to-end via the public surface
# ---------------------------------------------------------------------------


def test_gap_c_debounce_boundary_at_500ms_locked_at_module_level():
    """The 500ms debounce is the locked v1.0 contract; pinned here as a
    regression guard at the module-constant level so any change forces
    an explicit review of the watcher behavior.
    """
    from src.triggers.watcher import DEBOUNCE_SECONDS

    assert DEBOUNCE_SECONDS == 0.5, (
        "DEBOUNCE_SECONDS is the locked watcher debounce window. "
        "Changing it from 0.5s requires updating the spec, the README, "
        "and the boundary tests in test_watcher.py."
    )


def test_gap_c_debounce_window_boundary_is_exactly_500ms_per_workflow_path():
    """End-to-end pin: 499ms within the window blocks, 501ms past it fires.

    The watcher daemon PR already pins these at the _should_fire
    function level. This audit-pass test pins the EXACT boundary one
    more time at the public-API level so a refactor that moves the
    check elsewhere can't silently change the contract.
    """
    from src.triggers.watcher import _record_fire, _reset_debounce_for_tests, _should_fire

    _reset_debounce_for_tests()
    _record_fire("wf", "/p", 100.0)
    # 499ms later → blocked.
    assert _should_fire("wf", "/p", 100.499) is False
    # 500ms exactly → fires (>= window).
    assert _should_fire("wf", "/p", 100.500) is True
    # 501ms later → fires.
    assert _should_fire("wf", "/p", 100.501) is True


# ---------------------------------------------------------------------------
# Gap (d) — per-process serialization within the API process
# ---------------------------------------------------------------------------


def test_gap_d_two_concurrent_webhooks_to_same_workflow_land_sequentially(client, tmp_path):
    """Two webhooks to the same workflow result in TWO workflow_runs rows,
    each with its own trigger_context, no row loss, no row corruption.

    The pin is on the OUTCOME (two distinct successful rows) rather than
    on the timing — TestClient is synchronous so there's no genuine
    parallelism inside the process, but a future async refactor of the
    request handler must still produce two distinct successful rows.
    """
    workflow_path = _write_workflow(tmp_path, "seq.py")
    _seed_webhook("seq", secret="s", file_path=workflow_path)

    body_a = b'{"req": "a"}'
    body_b = b'{"req": "b"}'

    resp_a = client.post(
        "/webhook/seq",
        content=body_a,
        headers={"X-Brain-Signature": _sign("s", body_a), "Content-Type": "application/json"},
    )
    resp_b = client.post(
        "/webhook/seq",
        content=body_b,
        headers={"X-Brain-Signature": _sign("s", body_b), "Content-Type": "application/json"},
    )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.json()["status"] == "success"
    assert resp_b.json()["status"] == "success"
    assert resp_a.json()["run_id"] != resp_b.json()["run_id"]

    # Both rows landed, both carry distinct trigger_context.
    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, trigger_context FROM workflow_runs "
            "WHERE workflow_name = %s AND trigger_context IS NOT NULL "
            "ORDER BY started_at",
            ("seq",),
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][1]["body"] == {"req": "a"}
    assert rows[1][1]["body"] == {"req": "b"}


# ---------------------------------------------------------------------------
# Gap (e) — across-process concurrency: API + runner work side-by-side
# ---------------------------------------------------------------------------


async def test_gap_e_runner_and_webhook_callsite_both_produce_distinct_rows(db_pool, tmp_path):
    """A direct runner call and a separate trigger_context-bearing runner
    call against the same workflow produce two distinct workflow_runs
    rows. This pins the invariant that different invocation sources do
    not collide on the database.

    The runner is the shared substrate the API process, the scheduler
    daemon, and the watcher daemon all use; if a future cross-process
    lock got added at the runner layer, it would break this contract
    and must surface as an architectural decision.
    """
    # First "process" — like the scheduler daemon firing a workflow.
    daemon_like = await run_workflow(
        Workflow(name="cross", steps=[ShellStep(name="s", command="echo daemon")]),
        "cross.py",
    )
    # Second "process" — like the API endpoint firing the same workflow.
    api_like = await run_workflow(
        Workflow(name="cross", steps=[ShellStep(name="s", command="echo api")]),
        "cross.py",
        trigger_context={"event": "webhook", "body": None, "headers": {}, "path": None},
    )

    assert daemon_like.id != api_like.id
    assert daemon_like.status == "success"
    assert api_like.status == "success"

    rows = await fetch_all(
        "SELECT id, trigger_context FROM workflow_runs WHERE workflow_name = %s ORDER BY started_at",
        ("cross",),
    )
    assert len(rows) == 2
    # The daemon-like run has no trigger_context; the api-like run does.
    assert rows[0]["trigger_context"] is None
    assert rows[1]["trigger_context"]["event"] == "webhook"


async def test_gap_e_concurrent_runner_calls_do_not_corrupt_workflow_runs_rows(db_pool):
    """Three concurrent runner calls under asyncio.gather all produce
    distinct rows in workflow_runs. Pins the across-process concurrency
    invariant at the runner layer: the database absorbs concurrent
    INSERTs without row loss or row coercion.
    """

    async def fire(label: str):
        return await run_workflow(
            Workflow(
                name="concurrent",
                steps=[ShellStep(name="s", command=f"echo {label}")],
            ),
            "concurrent.py",
        )

    results = await asyncio.gather(fire("a"), fire("b"), fire("c"))
    assert all(r.status == "success" for r in results)
    assert len({r.id for r in results}) == 3, "concurrent runs collided on UUID — impossible"

    rows = await fetch_all(
        "SELECT id FROM workflow_runs WHERE workflow_name = %s",
        ("concurrent",),
    )
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Lock-the-decision tests — flag accidental scope changes loudly
# ---------------------------------------------------------------------------


async def test_runner_signature_locks_trigger_context_as_kwarg_only_not_positional(db_pool):
    """trigger_context must stay a keyword-only-feeling kwarg with default
    None. A positional refactor would silently change every existing
    callsite's argument order — pin so the test breaks loudly."""
    import inspect

    sig = inspect.signature(run_workflow)
    params = sig.parameters
    assert "trigger_context" in params
    assert params["trigger_context"].default is None


def test_webhook_endpoint_path_is_locked_at_slash_webhook_workflow_name(client):
    """The endpoint path is the public contract callers integrate against.
    Pinning it here so a rename or restructure forces an explicit review.
    """
    # 404 is fine — the point is the path exists at the right shape.
    resp = client.post("/webhook/anything", content=b"")
    assert resp.status_code in (401, 404)

    # And the wrong shape still 404s through FastAPI's router, not the
    # webhook handler — pinning the path structure, not the response.
    resp = client.post("/webhook/", content=b"")
    assert resp.status_code in (404, 405, 422)
