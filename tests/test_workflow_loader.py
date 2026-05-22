"""Tests for the workflow models and the workflow file loader.

The loader executes a plain Python file and returns its validated
``Workflow``. These tests cover model validation (the rules a workflow
author can trip) and every loader failure mode.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.workflow.loader import WorkflowLoadError, import_workflow_from_file
from src.workflow.models import LLMStep, MemoryVaultStep, ShellStep, Workflow

# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_valid_workflow_with_all_three_step_types():
    workflow = Workflow(
        name="digest",
        steps=[
            MemoryVaultStep(name="recent", query="this week", space="work"),
            LLMStep(name="summarize", prompt="Summarize: {recent}"),
            ShellStep(name="save", command="cat > out.md"),
        ],
    )
    assert workflow.name == "digest"
    assert [s.type for s in workflow.steps] == ["memory_vault", "llm", "shell"]


def test_workflow_requires_at_least_one_step():
    with pytest.raises(ValidationError):
        Workflow(name="empty", steps=[])


def test_workflow_name_rejects_leading_trailing_whitespace():
    with pytest.raises(ValidationError, match="leading/trailing whitespace"):
        Workflow(name=" padded ", steps=[ShellStep(name="s", command="echo hi")])


def test_workflow_rejects_duplicate_step_names():
    with pytest.raises(ValidationError, match="duplicate step name"):
        Workflow(
            name="dup",
            steps=[
                ShellStep(name="same", command="echo a"),
                ShellStep(name="same", command="echo b"),
            ],
        )


def test_step_rejects_unknown_field():
    # extra="forbid" — a typo'd config key fails at authoring time.
    with pytest.raises(ValidationError):
        ShellStep(name="s", command="echo hi", timeoutt=5)


def test_step_name_must_be_non_empty():
    with pytest.raises(ValidationError):
        ShellStep(name="", command="echo hi")


def test_memory_vault_limit_bounds():
    with pytest.raises(ValidationError):
        MemoryVaultStep(name="q", query="x", limit=0)
    with pytest.raises(ValidationError):
        MemoryVaultStep(name="q", query="x", limit=101)


def test_llm_temperature_bounds():
    with pytest.raises(ValidationError):
        LLMStep(name="g", prompt="hi", temperature=2.5)


def test_discriminated_union_picks_subclass_by_type():
    # A Workflow parsed from a dict resolves each step to its concrete class.
    workflow = Workflow.model_validate(
        {
            "name": "mixed",
            "steps": [
                {"type": "shell", "name": "s", "command": "echo hi"},
                {"type": "llm", "name": "g", "prompt": "hi"},
            ],
        }
    )
    assert isinstance(workflow.steps[0], ShellStep)
    assert isinstance(workflow.steps[1], LLMStep)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_loads_a_valid_workflow_file(tmp_path):
    path = _write(
        tmp_path,
        "wf.py",
        "from src.workflow import Workflow, ShellStep\n"
        "workflow = Workflow(name='wf', steps=[ShellStep(name='s', command='echo hi')])\n",
    )
    workflow = import_workflow_from_file(path)
    assert isinstance(workflow, Workflow)
    assert workflow.name == "wf"


def test_missing_file_raises_load_error(tmp_path):
    with pytest.raises(WorkflowLoadError, match="not found"):
        import_workflow_from_file(tmp_path / "nope.py")


def test_non_py_file_raises_load_error(tmp_path):
    path = _write(tmp_path, "wf.txt", "workflow = None\n")
    with pytest.raises(WorkflowLoadError, match="must be a .py file"):
        import_workflow_from_file(path)


def test_file_with_no_workflow_variable_raises(tmp_path):
    path = _write(tmp_path, "wf.py", "x = 1\n")
    with pytest.raises(WorkflowLoadError, match="no module-level 'workflow'"):
        import_workflow_from_file(path)


def test_workflow_variable_wrong_type_raises(tmp_path):
    path = _write(tmp_path, "wf.py", "workflow = 'not a workflow'\n")
    with pytest.raises(WorkflowLoadError, match="must be a Workflow instance"):
        import_workflow_from_file(path)


def test_file_that_raises_on_import_is_wrapped(tmp_path):
    path = _write(tmp_path, "wf.py", "raise RuntimeError('boom')\n")
    with pytest.raises(WorkflowLoadError, match="error executing workflow file"):
        import_workflow_from_file(path)


def test_file_with_invalid_workflow_config_is_wrapped(tmp_path):
    # A validation error inside the file surfaces as a load error.
    path = _write(
        tmp_path,
        "wf.py",
        "from src.workflow import Workflow\nworkflow = Workflow(name='x', steps=[])\n",
    )
    with pytest.raises(WorkflowLoadError, match="error executing workflow file"):
        import_workflow_from_file(path)
