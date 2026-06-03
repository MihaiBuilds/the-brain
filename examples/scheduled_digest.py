"""Example workflow — a daily digest that reads its own previous run.

Pulls recent memories, summarizes them with an LLM, and saves the summary.
Each run can reference the prior successful run's output via the
``{previous.<step_name>}`` placeholder — useful for workflows that build
on themselves over time, like "yesterday's summary plus what's new today."

Register it on a cron schedule:

    brain register examples/scheduled_digest.py --cron "0 9 * * *"

Then `brain list` confirms it's scheduled, and the daemon (running as
PID 1 in the container) fires it at 09:00 UTC every day. Past runs land
in `brain history` like any other workflow.
"""

from src.workflow import LLMStep, MemoryVaultStep, ShellStep, Workflow

workflow = Workflow(
    name="scheduled-digest",
    steps=[
        MemoryVaultStep(
            name="recent",
            query="what happened today",
            space="work",
            limit=20,
        ),
        LLMStep(
            name="summary",
            system=(
                "You write concise daily digests. Build on yesterday's "
                "summary by noting what's new or has changed."
            ),
            prompt=(
                "Yesterday's summary:\n{previous.summary}\n\n"
                "Today's memories:\n{recent}\n\n"
                "Write today's summary."
            ),
        ),
        ShellStep(
            name="save",
            command="cat > /tmp/digest.md",
        ),
    ],
)
