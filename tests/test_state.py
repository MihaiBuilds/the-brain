"""End-to-end tests for ``{previous.X}`` placeholder substitution and run linkage.

Like ``test_runner.py``, these run against the real test Postgres — the
contract is what gets written to ``workflow_runs`` (the ``previous_run_id``
linkage, the substituted output) just as much as what the runner returns.

Workflows here use only ShellStep so they are hermetic without mocking HTTP.
"""

from src.db import fetch_one
from src.runner import run_workflow
from src.workflow.models import ShellStep, Workflow


async def _fetch_run(run_id):
    return await fetch_one("SELECT * FROM workflow_runs WHERE id = %s", (run_id,))


# ---------------------------------------------------------------------------
# {previous.X} — happy paths
# ---------------------------------------------------------------------------


async def test_previous_placeholder_resolves_to_prior_successful_step_output(db_pool):
    """A second run reads the first run's step output via {previous.step_name}."""
    first = await run_workflow(
        Workflow(
            name="recall",
            steps=[ShellStep(name="emit", command="echo banked-value")],
        ),
        "recall.py",
    )
    assert first.status == "success"

    second = await run_workflow(
        Workflow(
            name="recall",
            steps=[ShellStep(name="read", command="echo got {previous.emit}")],
        ),
        "recall.py",
    )

    assert second.status == "success"
    row = await _fetch_run(second.id)
    read = next(s for s in row["output"] if s["name"] == "read")
    assert read["output"] == "got banked-value"


async def test_previous_run_id_links_to_last_successful_run(db_pool):
    """The runner populates previous_run_id whenever a prior successful run exists,
    regardless of whether the workflow uses {previous.X}.
    """
    first = await run_workflow(
        Workflow(name="linked", steps=[ShellStep(name="s", command="echo ok")]),
        "linked.py",
    )
    second = await run_workflow(
        Workflow(name="linked", steps=[ShellStep(name="s", command="echo ok")]),
        "linked.py",
    )

    assert second.previous_run_id == first.id
    row = await _fetch_run(second.id)
    assert row["previous_run_id"] == first.id


async def test_previous_run_id_is_null_on_the_very_first_run(db_pool):
    """No prior runs at all → previous_run_id stays NULL."""
    result = await run_workflow(
        Workflow(name="first-ever", steps=[ShellStep(name="s", command="echo hi")]),
        "first.py",
    )

    assert result.previous_run_id is None
    row = await _fetch_run(result.id)
    assert row["previous_run_id"] is None


async def test_previous_run_id_skips_failed_runs_to_find_last_success(db_pool):
    """The lookup is for the last SUCCESSFUL run, not the last run."""
    success = await run_workflow(
        Workflow(name="skipper", steps=[ShellStep(name="s", command="echo ok")]),
        "skipper.py",
    )
    assert success.status == "success"

    failed = await run_workflow(
        Workflow(name="skipper", steps=[ShellStep(name="s", command="exit 1")]),
        "skipper.py",
    )
    assert failed.status == "failed"

    # The failed run itself links back to the prior success.
    assert failed.previous_run_id == success.id

    # The next run should ALSO link to the original success, not the failure.
    next_run = await run_workflow(
        Workflow(name="skipper", steps=[ShellStep(name="s", command="echo ok again")]),
        "skipper.py",
    )
    assert next_run.previous_run_id == success.id


async def test_previous_lookup_is_scoped_per_workflow_name(db_pool):
    """A successful run of workflow A does not leak into workflow B's previous lookup."""
    await run_workflow(
        Workflow(name="a", steps=[ShellStep(name="s", command="echo a-output")]),
        "a.py",
    )
    b_run = await run_workflow(
        Workflow(name="b", steps=[ShellStep(name="s", command="echo b-output")]),
        "b.py",
    )

    assert b_run.previous_run_id is None


# ---------------------------------------------------------------------------
# {previous.X} — failure paths (strict by design)
# ---------------------------------------------------------------------------


async def test_previous_placeholder_without_prior_successful_run_fails_that_step(db_pool):
    """{previous.X} on the very first run fails the step (strict, matches M1 voice)."""
    result = await run_workflow(
        Workflow(
            name="orphan",
            steps=[ShellStep(name="read", command="echo {previous.nothing}")],
        ),
        "orphan.py",
    )

    assert result.status == "failed"
    assert "previous" in result.error
    assert "no previous successful run" in result.error

    row = await _fetch_run(result.id)
    assert row["output"][0]["success"] is False


async def test_previous_placeholder_with_missing_step_in_previous_run_fails_that_step(db_pool):
    """A previous successful run that has no step with the named identifier fails the step."""
    await run_workflow(
        Workflow(
            name="evolving",
            steps=[ShellStep(name="emit-v1", command="echo old-shape")],
        ),
        "evolving.py",
    )

    result = await run_workflow(
        Workflow(
            name="evolving",
            steps=[ShellStep(name="read", command="echo {previous.emit-v2}")],
        ),
        "evolving.py",
    )

    assert result.status == "failed"
    assert "previous" in result.error
    assert "emit-v2" in result.error


# ---------------------------------------------------------------------------
# Mixing tokens — {previous.X} alongside {prior_step_name} in one run
# ---------------------------------------------------------------------------


async def test_previous_and_prior_step_tokens_resolve_in_same_step(db_pool):
    """A step can mix {previous.X} (cross-run) and {Y} (intra-run) freely."""
    await run_workflow(
        Workflow(
            name="mixed",
            steps=[ShellStep(name="from-yesterday", command="echo banked")],
        ),
        "mixed.py",
    )

    result = await run_workflow(
        Workflow(
            name="mixed",
            steps=[
                ShellStep(name="from-today", command="echo fresh"),
                ShellStep(
                    name="combine",
                    command="echo y={previous.from-yesterday} t={from-today}",
                ),
            ],
        ),
        "mixed.py",
    )

    assert result.status == "success"
    row = await _fetch_run(result.id)
    combine = next(s for s in row["output"] if s["name"] == "combine")
    assert combine["output"] == "y=banked t=fresh"


async def test_previous_resolves_correct_step_when_previous_run_had_many_steps(db_pool):
    """A successful run with N steps exposes ALL of them to {previous.X} — pick the right one."""
    await run_workflow(
        Workflow(
            name="multi",
            steps=[
                ShellStep(name="alpha", command="echo from-alpha"),
                ShellStep(name="bravo", command="echo from-bravo"),
                ShellStep(name="charlie", command="echo from-charlie"),
            ],
        ),
        "multi.py",
    )

    # The follow-up run reads exactly one of the previous run's steps —
    # the lookup must find it among siblings, not just return the first.
    result = await run_workflow(
        Workflow(
            name="multi",
            steps=[ShellStep(name="reader", command="echo got {previous.bravo}")],
        ),
        "multi.py",
    )

    assert result.status == "success"
    row = await _fetch_run(result.id)
    reader = next(s for s in row["output"] if s["name"] == "reader")
    assert reader["output"] == "got from-bravo"
