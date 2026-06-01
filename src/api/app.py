"""
The Brain — HTTP surface.

A single endpoint: ``POST /run`` accepts a ``{"workflow_path": "..."}``
body, loads the workflow from that path, runs it synchronously, and
returns the persisted run's metadata as JSON. The request blocks until
the workflow finishes.

Auth
----
Bearer-token from the ``Authorization: Bearer <token>`` header,
constant-time compared against the ``THE_BRAIN_API_TOKEN`` env var.
``create_app`` raises ``RuntimeError`` if the variable is unset — the
server refuses to start without auth so the endpoint never runs open.

Threat model — v1.0
-------------------
Single token. Single user. Server-to-server. No CORS. Designed to be
called from a known caller on the same network, never from a browser.
The endpoint imports an arbitrary server-side Python file from a path
the caller provides — meaning anyone with the token can execute
arbitrary code on the host. The token is the only gate; treat it as a
production secret. Path allowlisting and per-caller scoping are
post-v1.0 concerns.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.db import close_pool, init_pool
from src.runner import run_workflow
from src.workflow.loader import WorkflowLoadError, import_workflow_from_file

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "THE_BRAIN_API_TOKEN"


class RunRequest(BaseModel):
    """Body for ``POST /run``."""

    workflow_path: str


class RunResponse(BaseModel):
    """200 body for ``POST /run`` — both successful and failed workflow runs.

    A failed workflow is still a successful API call: the server did its
    job, the workflow ran, it just didn't succeed. ``error`` carries the
    failure reason in that case (none on success).
    """

    run_id: str
    status: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    error: str | None = None


def create_app(manage_pool: bool = True) -> FastAPI:
    """Build the FastAPI app, binding the bearer token from the env var.

    Args:
        manage_pool: open + close the DB pool in the app's lifespan.
            True for production (uvicorn owns the event loop and there
            is no outer pool). False for tests, which run against the
            session-scoped pool from ``conftest.py`` and would deadlock
            trying to open a second one.

    Raises:
        RuntimeError: ``THE_BRAIN_API_TOKEN`` is unset or empty. The
            server refuses to start with no auth so the endpoint cannot
            accidentally run open.
    """
    expected_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not expected_token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set — refusing to start the HTTP API without auth"
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_pool:
            await init_pool()
        try:
            yield
        finally:
            if manage_pool:
                await close_pool()

    app = FastAPI(
        title="The Brain",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    bearer = HTTPBearer(auto_error=False)

    def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> None:
        """Validate the bearer token in constant time. Raises 401 on mismatch."""
        if credentials is None or credentials.scheme.lower() != "bearer":
            logger.warning("Auth rejected: missing bearer credentials")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not secrets.compare_digest(credentials.credentials, expected_token):
            logger.warning("Auth rejected: invalid token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post("/run", response_model=RunResponse, status_code=status.HTTP_200_OK)
    async def run_endpoint(
        body: RunRequest,
        _auth: None = Depends(require_token),  # noqa: B008
    ) -> RunResponse:
        """Load the workflow file, run it synchronously, return the run row."""
        try:
            workflow = import_workflow_from_file(body.workflow_path)
        except WorkflowLoadError as e:
            logger.info("Workflow load failed for %r: %s", body.workflow_path, e)
            raise HTTPException(status_code=400, detail=str(e)) from e

        try:
            workflow_run = await run_workflow(workflow, body.workflow_path)
        except Exception as e:
            # The runner is engineered to ALWAYS persist a terminal row even
            # when an executor crashes — see test_runner.py. Reaching here
            # means something outside the runner failed (DB pool, network).
            logger.exception("Runner raised unexpectedly for %r", body.workflow_path)
            raise HTTPException(status_code=500, detail=f"runner error: {e}") from e

        assert workflow_run.ended_at is not None  # set by the runner before return
        duration = (workflow_run.ended_at - workflow_run.started_at).total_seconds()

        return RunResponse(
            run_id=str(workflow_run.id),
            status=workflow_run.status,
            started_at=workflow_run.started_at,
            ended_at=workflow_run.ended_at,
            duration_seconds=duration,
            error=workflow_run.error,
        )

    return app
