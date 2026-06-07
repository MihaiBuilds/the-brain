"""Tests for the webhook HTTP endpoint at ``POST /webhook/{workflow_name}``.

Drives the endpoint through FastAPI's ``TestClient``. Each test creates a
fresh app — ``THE_BRAIN_API_TOKEN`` is set so ``create_app`` allows the
build (the token gates ``/run``, not ``/webhook``; the webhook endpoint
has its own per-row HMAC auth) — and the DB pool is opened/closed on
the TestClient's own event loop.

Webhook rows are seeded with a synchronous psycopg insert that records
both the secret and the absolute workflow file path. Tests then sign
the request body with the seeded secret and assert behavior end-to-end.
"""

from __future__ import annotations

import hashlib
import hmac
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from src.api.app import TOKEN_ENV_VAR, create_app

_VALID_TOKEN = "test-token-for-app-build"
_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)


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


def _fetch_run_trigger_context(run_id: str) -> dict | None:
    with psycopg.connect(_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT trigger_context FROM workflow_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    return row[0] if row else None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, _VALID_TOKEN)
    app = create_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth branches — signature, presence, malformed, disabled, unknown
# ---------------------------------------------------------------------------


def test_webhook_returns_200_on_valid_signature(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "ok.py")
    _seed_webhook("ok", secret="s", file_path=workflow_path)
    body = b'{"event": "push"}'
    resp = client.post(
        "/webhook/ok",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"


def test_webhook_returns_401_on_wrong_signature(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "deny.py")
    _seed_webhook("deny", secret="s", file_path=workflow_path)
    body = b"payload"
    resp = client.post(
        "/webhook/deny",
        content=body,
        headers={"X-Brain-Signature": _sign("wrong-secret", body)},
    )
    assert resp.status_code == 401


def test_webhook_returns_401_on_missing_signature_header(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "nosig.py")
    _seed_webhook("nosig", secret="s", file_path=workflow_path)
    resp = client.post("/webhook/nosig", content=b"payload")
    assert resp.status_code == 401


def test_webhook_returns_401_on_malformed_signature_header(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "garbled.py")
    _seed_webhook("garbled", secret="s", file_path=workflow_path)
    resp = client.post(
        "/webhook/garbled",
        content=b"payload",
        headers={"X-Brain-Signature": "totally-not-a-signature"},
    )
    assert resp.status_code == 401


def test_webhook_returns_404_on_unknown_workflow_name(client):
    """Unknown webhooks return 404 — never 401 — so existence is not leaked."""
    body = b"payload"
    resp = client.post(
        "/webhook/ghost",
        content=body,
        headers={"X-Brain-Signature": _sign("any", body)},
    )
    assert resp.status_code == 404


def test_webhook_disabled_returns_404_same_shape_as_unknown(client, tmp_path):
    """Disabled webhooks must respond identically to unknown ones."""
    workflow_path = _write_workflow(tmp_path, "off.py")
    _seed_webhook("off", secret="s", file_path=workflow_path, enabled=False)
    body = b"payload"
    resp = client.post(
        "/webhook/off",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body)},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Body shapes — JSON, raw string, empty
# ---------------------------------------------------------------------------


def test_webhook_with_json_body_records_parsed_object_in_trigger_context(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "jbody.py")
    _seed_webhook("jbody", secret="s", file_path=workflow_path)
    body = b'{"event": "push", "ref": "main"}'
    resp = client.post(
        "/webhook/jbody",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    ctx = _fetch_run_trigger_context(run_id)
    assert ctx is not None
    assert ctx["event"] == "webhook"
    assert ctx["body"] == {"event": "push", "ref": "main"}
    assert ctx["path"] is None


def test_webhook_with_non_json_body_records_raw_string(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "rbody.py")
    _seed_webhook("rbody", secret="s", file_path=workflow_path)
    body = b"this is not json"
    resp = client.post(
        "/webhook/rbody",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body)},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    ctx = _fetch_run_trigger_context(run_id)
    assert ctx["body"] == "this is not json"


def test_webhook_with_empty_body_records_none_body(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "ebody.py")
    _seed_webhook("ebody", secret="s", file_path=workflow_path)
    body = b""
    resp = client.post(
        "/webhook/ebody",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body)},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    ctx = _fetch_run_trigger_context(run_id)
    assert ctx["body"] is None


# ---------------------------------------------------------------------------
# Header allowlist — safe headers pass through, sensitive headers are dropped
# ---------------------------------------------------------------------------


def test_webhook_allowlist_passes_through_safe_headers(client, tmp_path):
    workflow_path = _write_workflow(tmp_path, "hdrs.py")
    _seed_webhook("hdrs", secret="s", file_path=workflow_path)
    body = b'{"a": 1}'
    resp = client.post(
        "/webhook/hdrs",
        content=body,
        headers={
            "X-Brain-Signature": _sign("s", body),
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Hookshot/abc123",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "uuid-here",
        },
    )
    assert resp.status_code == 200
    ctx = _fetch_run_trigger_context(resp.json()["run_id"])
    assert ctx["headers"]["content-type"] == "application/json"
    assert ctx["headers"]["user-agent"] == "GitHub-Hookshot/abc123"
    assert ctx["headers"]["x-github-event"] == "push"
    assert ctx["headers"]["x-github-delivery"] == "uuid-here"


def test_webhook_allowlist_drops_authorization_and_signature_headers(client, tmp_path):
    """Sensitive headers must not appear in trigger_context."""
    workflow_path = _write_workflow(tmp_path, "drops.py")
    _seed_webhook("drops", secret="s", file_path=workflow_path)
    body = b"{}"
    resp = client.post(
        "/webhook/drops",
        content=body,
        headers={
            "X-Brain-Signature": _sign("s", body),
            "Authorization": "Bearer some-other-token",
            "Cookie": "session=abc",
        },
    )
    assert resp.status_code == 200
    ctx = _fetch_run_trigger_context(resp.json()["run_id"])
    assert "authorization" not in ctx["headers"]
    assert "x-brain-signature" not in ctx["headers"]
    assert "cookie" not in ctx["headers"]


# ---------------------------------------------------------------------------
# Runner integration — the trigger_context column is populated
# ---------------------------------------------------------------------------


def test_webhook_persists_trigger_context_into_workflow_runs(client, tmp_path):
    """The runner stores the trigger_context dict on the workflow_runs row."""
    workflow_path = _write_workflow(tmp_path, "persist.py")
    _seed_webhook("persist", secret="s", file_path=workflow_path)
    body = b'{"x": "y"}'
    resp = client.post(
        "/webhook/persist",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    ctx = _fetch_run_trigger_context(resp.json()["run_id"])
    assert ctx is not None
    assert ctx["event"] == "webhook"
    # Round-trips as a dict, not as a JSON string.
    assert isinstance(ctx["body"], dict)
    assert ctx["body"]["x"] == "y"


def test_webhook_returns_400_when_registered_workflow_file_is_missing(client, tmp_path):
    """A registered workflow whose .py file was deleted yields a clean 400."""
    workflow_path = str(tmp_path / "vanished.py")  # never written
    _seed_webhook("vanished", secret="s", file_path=workflow_path)
    body = b"payload"
    resp = client.post(
        "/webhook/vanished",
        content=body,
        headers={"X-Brain-Signature": _sign("s", body)},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /docs surface stays hidden in production
# ---------------------------------------------------------------------------


def test_webhook_path_not_exposed_via_openapi_schema(client):
    """openapi.json stays 404 — the webhook endpoint must not advertise itself."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 404
    resp = client.get("/docs")
    assert resp.status_code == 404
