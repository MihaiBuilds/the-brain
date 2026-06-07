"""Example workflow — react to a markdown file change.

Fires when any file in the watched directory is modified. Reads the
changed file's path via the ``{trigger.path}`` placeholder and writes
a one-line summary to a log.

Register it as a file watcher:

    brain register-watcher examples/markdown_watcher.py \\
        --path /data/watched --events modified

Bring up the watcher profile so the daemon observes the directory:

    docker compose --profile watcher up -d

Then any file write inside ``./watched`` (mapped to ``/data/watched``
in the brain-watcher container via docker-compose.yml) triggers a run.
``brain history`` shows the run with its trigger context preserved in
the workflow_runs row, including the path of the file that changed.

Note: the watcher uses a 500ms debounce per (workflow, path) so a
single editor save that emits multiple filesystem events coalesces to
one workflow run.
"""

from src.workflow import ShellStep, Workflow

workflow = Workflow(
    name="markdown-watcher",
    steps=[
        ShellStep(
            name="noticed",
            command="echo file event={trigger.event} path={trigger.path}",
        ),
        ShellStep(
            name="log",
            command="echo {noticed} >> /tmp/markdown-watcher.log",
        ),
    ],
)
