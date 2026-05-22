"""End-to-end tests for ``run_workflow``.

These run against the real test Postgres — the runner's contract is as much
about what it writes to ``workflow_runs`` as about its return value, so every
test asserts on the persisted row.

Workflows here use only ShellStep so they are hermetic without mocking HTTP;
the executors' own HTTP paths are covered in ``test_executors.py``.
"""

from src.db import fetch_one
from src.runner import run_workflow
from src.workflow.models import ShellStep, Workflow


async def _fetch_run(run_id):
    return await fetch_one("SELECT * FROM workflow_runs WHERE id = %s", (run_id,))


async def test_successful_run_persists_a_success_row(db_pool):
    workflow = Workflow(
        name="ok",
        steps=[
            ShellStep(name="first", command="echo one"),
            ShellStep(name="second", command="echo two"),
        ],
    )
    result = await run_workflow(workflow, "ok.py")

    assert result.status == "success"
    assert result.error is None
    assert result.ended_at is not None

    row = await _fetch_run(result.id)
    assert row["status"] == "success"
    assert row["workflow_name"] == "ok"
    assert row["workflow_file_path"] == "ok.py"
    assert row["error"] is None


async def test_output_is_an_ordered_list_in_execution_order(db_pool):
    workflow = Workflow(
        name="ordered",
        steps=[
            ShellStep(name="alpha", command="echo a"),
            ShellStep(name="bravo", command="echo b"),
            ShellStep(name="charlie", command="echo c"),
        ],
    )
    result = await run_workflow(workflow, "ordered.py")

    row = await _fetch_run(result.id)
    # JSONB does not preserve object key order — output is a list so the
    # execution order is intrinsic to the data.
    assert [step["name"] for step in row["output"]] == ["alpha", "bravo", "charlie"]
    assert row["output"][0]["output"] == "a"
    assert all(step["success"] for step in row["output"])


async def test_failure_persists_failed_row_and_halts_remaining_steps(db_pool):
    workflow = Workflow(
        name="halts",
        steps=[
            ShellStep(name="ran", command="echo done"),
            ShellStep(name="boom", command="exit 1"),
            ShellStep(name="never", command="echo unreachable"),
        ],
    )
    result = await run_workflow(workflow, "halts.py")

    assert result.status == "failed"
    assert "boom" in result.error

    row = await _fetch_run(result.id)
    assert row["status"] == "failed"
    # The step after the failure never ran — only two entries are recorded.
    names = [step["name"] for step in row["output"]]
    assert names == ["ran", "boom"]
    assert "never" not in names


async def test_placeholder_substitution_passes_prior_output_forward(db_pool):
    workflow = Workflow(
        name="chained",
        steps=[
            ShellStep(name="produce", command="echo forty-two"),
            ShellStep(name="consume", command="echo got {produce}"),
        ],
    )
    result = await run_workflow(workflow, "chained.py")

    assert result.status == "success"
    row = await _fetch_run(result.id)
    consume = next(s for s in row["output"] if s["name"] == "consume")
    assert consume["output"] == "got forty-two"


async def test_unknown_placeholder_fails_that_step(db_pool):
    workflow = Workflow(
        name="bad-placeholder",
        steps=[ShellStep(name="consume", command="echo {nonexistent}")],
    )
    result = await run_workflow(workflow, "bad.py")

    assert result.status == "failed"
    assert "unknown placeholder" in result.error

    row = await _fetch_run(result.id)
    consume = row["output"][0]
    assert consume["success"] is False


async def test_on_step_complete_callback_fires_once_per_step(db_pool):
    seen = []
    workflow = Workflow(
        name="callback",
        steps=[
            ShellStep(name="one", command="echo 1"),
            ShellStep(name="two", command="echo 2"),
        ],
    )
    await run_workflow(workflow, "cb.py", on_step_complete=seen.append)

    assert [r.step_name for r in seen] == ["one", "two"]
    assert all(r.success for r in seen)


async def test_callback_fires_for_the_failing_step_then_stops(db_pool):
    seen = []
    workflow = Workflow(
        name="callback-halt",
        steps=[
            ShellStep(name="ok", command="echo ok"),
            ShellStep(name="bad", command="exit 1"),
            ShellStep(name="skipped", command="echo nope"),
        ],
    )
    await run_workflow(workflow, "cbh.py", on_step_complete=seen.append)

    assert [r.step_name for r in seen] == ["ok", "bad"]
    assert seen[-1].success is False
