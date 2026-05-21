"""The persisted record of one workflow execution.

``WorkflowRun`` mirrors the ``workflow_runs`` table — one row per run.
The runner creates it ``running``, then updates it to a terminal status.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class WorkflowRun(BaseModel):
    """One workflow execution, as stored in the ``workflow_runs`` table.

    ``output`` is a dict keyed by step name — each value is that step's
    ``{success, output}``. It is populated as steps complete, so a failed
    run still carries the results of the steps that ran before the failure.
    """

    id: UUID
    workflow_name: str
    workflow_file_path: str
    started_at: datetime
    ended_at: datetime | None = None
    status: str
    output: dict[str, dict] | None = None
    error: str | None = None
