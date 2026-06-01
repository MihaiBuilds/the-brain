"""Tests for the HTTP API in ``src.api.app``.

Drives ``POST /run`` through FastAPI's ``TestClient``. Each test creates a
fresh app — ``THE_BRAIN_API_TOKEN`` is bound at app-build time, and the
DB pool is opened/closed by the app's lifespan on the TestClient's own
event loop. No shared async fixture is used; this keeps the HTTP tests
self-contained.

Workflows are written to ``tmp_path`` so each test is hermetic.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from src.api.app import TOKEN_ENV_VAR, create_app

_VALID_TOKEN = "test-token-abc-123"


def _write_workflow(tmp_path, name, workflow_name=None, command="echo hi"):
    wf_name = workflow_name or name.removesuffix(".py")
    path = tmp_path / name
    path.write_text(
        "from src.workflow import Workflow, ShellStep\n"
        f"workflow = Workflow(name='{wf_name}', steps=[ShellStep(name='s', command='{command}')])\n"
    )
    return str(path)


@pytest.fixture
def client(monkeypatch):
    """A TestClient bound to a fresh app with the test token set."""
    monkeypatch.setenv(TOKEN_ENV_VAR, _VALID_TOKEN)
    app = create_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# create_app — refuses to build without the token
# ---------------------------------------------------------------------------


def test_create_app_raises_when_token_env_var_is_unset(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        create_app()


def test_create_app_raises_when_token_env_var_is_empty(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "   ")
    with pytest.raises(RuntimeError, match="not set"):
        create_app()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_authorization_header_returns_401(client, tmp_path):
    path = _write_workflow(tmp_path, "ok.py")
    response = client.post("/run", json={"workflow_path": path})

    assert response.status_code == 401
    assert "missing bearer token" in response.json()["detail"]


def test_wrong_token_returns_401(client, tmp_path):
    path = _write_workflow(tmp_path, "ok.py")
    response = client.post(
        "/run",
        json={"workflow_path": path},
        headers={"Authorization": "Bearer not-the-real-token"},
    )

    assert response.status_code == 401
    assert "invalid token" in response.json()["detail"]


def test_non_bearer_scheme_returns_401(client, tmp_path):
    path = _write_workflow(tmp_path, "ok.py")
    response = client.post(
        "/run",
        json={"workflow_path": path},
        headers={"Authorization": f"Basic {_VALID_TOKEN}"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /run — happy and failure paths
# ---------------------------------------------------------------------------


def test_valid_run_returns_200_with_run_metadata(client, tmp_path):
    path = _write_workflow(tmp_path, "ok.py")
    response = client.post(
        "/run",
        json={"workflow_path": path},
        headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["error"] is None
    assert body["duration_seconds"] >= 0
    # run_id is a UUID string.
    assert len(body["run_id"]) == 36
    # Both timestamps are ISO 8601 strings.
    assert "T" in body["started_at"]
    assert "T" in body["ended_at"]


def test_failed_workflow_returns_200_with_status_failed_and_error(client, tmp_path):
    """A failed workflow is still a successful API call — the server did its job."""
    path = _write_workflow(tmp_path, "bad.py", command="exit 1")
    response = client.post(
        "/run",
        json={"workflow_path": path},
        headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] is not None
    assert "failed" in body["error"]


def test_unloadable_workflow_path_returns_400(client, tmp_path):
    missing = str(tmp_path / "ghost.py")
    response = client.post(
        "/run",
        json={"workflow_path": missing},
        headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
    )

    assert response.status_code == 400
    # The WorkflowLoadError message is surfaced as the detail.
    assert response.json()["detail"]


def test_missing_workflow_path_field_returns_422(client):
    """Pydantic body validation — empty body or wrong shape is 422 (FastAPI default)."""
    response = client.post(
        "/run",
        json={},
        headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Docs are disabled — no /docs, /redoc, /openapi.json
# ---------------------------------------------------------------------------


def test_no_docs_endpoint_is_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# OS-env clobbering check — token comes from env at app-build time
# ---------------------------------------------------------------------------


def test_token_is_locked_at_app_build_time_not_per_request(monkeypatch, tmp_path):
    """Changing THE_BRAIN_API_TOKEN at runtime does not affect a running app.

    This is by design — the env var is read once during ``create_app``. A
    rotation requires a server restart.
    """
    monkeypatch.setenv(TOKEN_ENV_VAR, "build-time-token")
    app = create_app()

    with TestClient(app) as c:
        # Old token still works.
        path = _write_workflow(tmp_path, "ok.py")
        ok = c.post(
            "/run",
            json={"workflow_path": path},
            headers={"Authorization": "Bearer build-time-token"},
        )
        assert ok.status_code == 200, ok.text

        # Even if we "rotate" the env var mid-flight, the app keeps the original.
        os.environ[TOKEN_ENV_VAR] = "new-token-that-is-ignored"
        rotated = c.post(
            "/run",
            json={"workflow_path": path},
            headers={"Authorization": "Bearer new-token-that-is-ignored"},
        )
        assert rotated.status_code == 401
