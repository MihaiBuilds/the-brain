"""End-to-end tests for ``{trigger.X}`` placeholder substitution.

Like ``test_state.py``, these run against the real test Postgres — the
contract is what gets written to ``workflow_runs.output`` after the
runner substitutes the placeholder, just as much as what the runner
returns.

Workflows here use only ShellStep so they are hermetic without mocking
HTTP. The trigger_context dict is passed directly to ``run_workflow``,
the same way the webhook endpoint and (soon) the file watcher daemon
pass it.
"""

from src.db import fetch_one
from src.runner import run_workflow
from src.workflow.models import ShellStep, Workflow


async def _fetch_run(run_id):
    return await fetch_one("SELECT * FROM workflow_runs WHERE id = %s", (run_id,))


def _shell_workflow(name: str, command: str) -> Workflow:
    return Workflow(name=name, steps=[ShellStep(name="echo", command=command)])


# ---------------------------------------------------------------------------
# {trigger.event} — the simplest token
# ---------------------------------------------------------------------------


async def test_trigger_event_resolves_to_webhook(db_pool):
    result = await run_workflow(
        _shell_workflow("t-event", "echo got {trigger.event}"),
        "t.py",
        trigger_context={"event": "webhook", "body": None, "headers": {}, "path": None},
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "got webhook"


async def test_trigger_event_resolves_to_file_for_file_trigger(db_pool):
    result = await run_workflow(
        _shell_workflow("t-file", "echo got {trigger.event}"),
        "t.py",
        trigger_context={"event": "file", "body": None, "headers": {}, "path": "/tmp/x"},
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "got file"


# ---------------------------------------------------------------------------
# {trigger.path} — null for webhooks, populated for file events
# ---------------------------------------------------------------------------


async def test_trigger_path_resolves_to_file_path(db_pool):
    result = await run_workflow(
        _shell_workflow("t-path", "echo changed {trigger.path}"),
        "t.py",
        trigger_context={
            "event": "file",
            "body": None,
            "headers": {},
            "path": "/data/in/note.md",
        },
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "changed /data/in/note.md"


async def test_trigger_path_resolves_to_empty_string_when_null(db_pool):
    """A webhook run has path=None — the placeholder substitutes to empty string."""
    result = await run_workflow(
        _shell_workflow("t-nullpath", "echo path=[{trigger.path}]"),
        "t.py",
        trigger_context={"event": "webhook", "body": None, "headers": {}, "path": None},
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "path=[]"


# ---------------------------------------------------------------------------
# {trigger.body} — JSON object stringified, raw string passed through
# ---------------------------------------------------------------------------


async def test_trigger_body_string_passes_through_unchanged(db_pool):
    result = await run_workflow(
        _shell_workflow("t-rawbody", "echo got {trigger.body}"),
        "t.py",
        trigger_context={
            "event": "webhook",
            "body": "plain text payload",
            "headers": {},
            "path": None,
        },
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "got plain text payload"


async def test_trigger_body_json_object_is_stringified_deterministically(db_pool):
    """A dict body comes through as JSON with sorted keys + compact separators.

    The resolver returns the serialized JSON string; downstream the shell
    sees the substituted command literally. We quote the placeholder to
    keep the shell from interpreting the braces, so this asserts the
    substitution itself produced the expected JSON.
    """
    result = await run_workflow(
        _shell_workflow("t-jsonbody", "echo '{trigger.body}'"),
        "t.py",
        trigger_context={
            "event": "webhook",
            "body": {"ref": "main", "action": "push"},
            "headers": {},
            "path": None,
        },
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    # Sorted-key deterministic output: action before ref.
    assert out["output"] == '{"action":"push","ref":"main"}'


# ---------------------------------------------------------------------------
# {trigger.headers.X} — case-insensitive lookup
# ---------------------------------------------------------------------------


async def test_trigger_headers_resolves_case_insensitively(db_pool):
    """HTTP headers are case-insensitive — the placeholder lookup honors that."""
    result = await run_workflow(
        _shell_workflow("t-hdr", "echo event={trigger.headers.X-Github-Event}"),
        "t.py",
        trigger_context={
            "event": "webhook",
            "body": None,
            # Stored lowercase as the endpoint emits them; lookup uses the
            # exact case the user wrote.
            "headers": {"x-github-event": "push"},
            "path": None,
        },
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "event=push"


# ---------------------------------------------------------------------------
# Strict-failure paths
# ---------------------------------------------------------------------------


async def test_trigger_token_on_manual_run_fails_the_step(db_pool):
    """No trigger_context = the step references trigger data that doesn't exist."""
    result = await run_workflow(
        _shell_workflow("t-manual", "echo {trigger.body}"),
        "t.py",
        # trigger_context omitted — defaults to None.
    )
    assert result.status == "failed"
    assert "trigger" in result.error
    assert "was not invoked by a trigger" in result.error


async def test_trigger_unknown_header_fails_the_step(db_pool):
    """Missing-by-name header is a clear, specific failure."""
    result = await run_workflow(
        _shell_workflow("t-no-hdr", "echo {trigger.headers.X-Missing-Name}"),
        "t.py",
        trigger_context={
            "event": "webhook",
            "body": None,
            "headers": {"content-type": "application/json"},
            "path": None,
        },
    )
    assert result.status == "failed"
    assert "X-Missing-Name" in result.error


async def test_trigger_unknown_field_name_fails_the_step(db_pool):
    """A token like {trigger.unknown} is rejected, not silently coerced."""
    result = await run_workflow(
        _shell_workflow("t-unknown", "echo {trigger.unknown}"),
        "t.py",
        trigger_context={"event": "webhook", "body": None, "headers": {}, "path": None},
    )
    assert result.status == "failed"
    assert "unknown" in result.error


async def test_trigger_headers_with_dotted_name_is_rejected(db_pool):
    """`.` is the resolver delimiter — header names with dots are ambiguous."""
    result = await run_workflow(
        _shell_workflow("t-dot", "echo {trigger.headers.x-with.dots}"),
        "t.py",
        trigger_context={
            "event": "webhook",
            "body": None,
            "headers": {"x-with.dots": "value"},
            "path": None,
        },
    )
    assert result.status == "failed"
    assert "'.'" in result.error or "dot" in result.error.lower()


# ---------------------------------------------------------------------------
# Mixed-token resolution — {previous.X} + {trigger.Y} + {step_name}
# ---------------------------------------------------------------------------


async def test_trigger_mixes_with_intra_run_step_token(db_pool):
    """A step can read both an earlier step's output AND a trigger field."""
    result = await run_workflow(
        Workflow(
            name="t-mix",
            steps=[
                ShellStep(name="first", command="echo from-step"),
                ShellStep(
                    name="combine",
                    command="echo step={first} event={trigger.event}",
                ),
            ],
        ),
        "t.py",
        trigger_context={"event": "webhook", "body": None, "headers": {}, "path": None},
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    combine = next(s for s in row["output"] if s["name"] == "combine")
    assert combine["output"] == "step=from-step event=webhook"


async def test_trigger_mixes_with_previous_token(db_pool):
    """A second run can read both {previous.X} and {trigger.X} in the same step."""
    await run_workflow(
        _shell_workflow("t-prevtrig", "echo banked-output"),
        "t.py",
    )
    result = await run_workflow(
        _shell_workflow(
            "t-prevtrig",
            "echo prior={previous.echo} live={trigger.event}",
        ),
        "t.py",
        trigger_context={"event": "webhook", "body": None, "headers": {}, "path": None},
    )
    assert result.status == "success"
    row = await _fetch_run(result.id)
    out = next(s for s in row["output"] if s["name"] == "echo")
    assert out["output"] == "prior=banked-output live=webhook"


# ---------------------------------------------------------------------------
# Nested JSON access NOT supported in v1.0
# ---------------------------------------------------------------------------


async def test_trigger_body_dot_field_is_treated_as_unknown_token(db_pool):
    """{trigger.body.foo.bar} is NOT a path walk — body is a string field.

    Locked behavior: the resolver sees `body.foo.bar` after the
    `trigger.` prefix is stripped and treats it as an unknown trigger
    field, not as JSON-path navigation. Nested access is a v1.1+ concern.
    """
    result = await run_workflow(
        _shell_workflow("t-deepjson", "echo {trigger.body.foo}"),
        "t.py",
        trigger_context={
            "event": "webhook",
            "body": {"foo": "bar"},
            "headers": {},
            "path": None,
        },
    )
    assert result.status == "failed"
    assert "body.foo" in result.error
