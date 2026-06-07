"""The persisted record of one workflow execution.

``WorkflowRun`` mirrors the ``workflow_runs`` table — one row per run.
The runner creates it ``running``, then updates it to a terminal status.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class WorkflowRun(BaseModel):
    """One workflow execution, as stored in the ``workflow_runs`` table.

    ``output`` is an ordered list — one entry per step that ran, in
    execution order, each ``{name, success, output}``. It is populated
    as steps complete, so a failed run still carries the results of the
    steps that ran before the failure.

    ``planned_steps`` is the snapshot of the workflow's step list at
    run-creation time, each ``{name, type}``. It lets postmortem
    disambiguate "step absent from output because halted before reaching
    it" from "step never existed in this workflow version."

    ``previous_run_id`` links to the most recent successful run of the
    same workflow at the moment this run started — populated whenever
    such a run exists, regardless of whether the workflow used a
    ``{previous.X}`` placeholder. Enables follow-the-chain postmortem.
    """

    id: UUID
    workflow_name: str
    workflow_file_path: str
    started_at: datetime
    ended_at: datetime | None = None
    status: str
    output: list[dict] | None = None
    error: str | None = None
    previous_run_id: UUID | None = None
    planned_steps: list[dict] | None = None
    trigger_context: dict | None = None
