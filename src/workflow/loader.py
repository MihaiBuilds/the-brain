"""
Workflow loader — imports a workflow file and returns its ``Workflow``.

A workflow file is plain Python: ``import_workflow_from_file`` executes it
and reads the module-level ``workflow`` variable. M1 is single-tenant and
self-hosted — the operator runs their own files, so there is no sandbox.
Every failure mode raises ``WorkflowLoadError`` with an actionable message.
"""

import importlib.util
import sys
from pathlib import Path

from src.workflow.models import Workflow


class WorkflowLoadError(Exception):
    """Raised when a workflow file cannot be loaded or is malformed."""


def import_workflow_from_file(path: str | Path) -> Workflow:
    """Load a workflow file and return its validated ``Workflow``.

    Args:
        path: Path to a ``.py`` file defining a module-level ``workflow``.

    Raises:
        WorkflowLoadError: file missing, not a ``.py`` file, import fails,
            no ``workflow`` variable, or it is not a valid ``Workflow``.
    """
    file_path = Path(path).expanduser().resolve()

    if not file_path.is_file():
        raise WorkflowLoadError(f"workflow file not found: {file_path}")
    if file_path.suffix != ".py":
        raise WorkflowLoadError(f"workflow file must be a .py file: {file_path}")

    module_name = f"_brain_workflow_{file_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise WorkflowLoadError(f"could not load workflow file: {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise WorkflowLoadError(f"error executing workflow file {file_path}: {e}") from e
    finally:
        sys.modules.pop(module_name, None)

    if not hasattr(module, "workflow"):
        raise WorkflowLoadError(
            f"workflow file {file_path} defines no module-level 'workflow' variable"
        )

    workflow = module.workflow
    if not isinstance(workflow, Workflow):
        raise WorkflowLoadError(
            f"'workflow' in {file_path} must be a Workflow instance, "
            f"got {type(workflow).__name__}"
        )

    return workflow
