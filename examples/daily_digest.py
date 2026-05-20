"""Example workflow — pull recent memories, summarize them, save to a file.

Run it with:  brain run examples/daily_digest.py   (available from sub-step 7)
"""

from src.workflow import LLMStep, MemoryVaultStep, ShellStep, Workflow

workflow = Workflow(
    name="daily-digest",
    steps=[
        MemoryVaultStep(
            name="recent",
            query="what happened this week",
            space="work",
            limit=10,
        ),
        LLMStep(
            name="summarize",
            system="You write concise daily digests.",
            prompt="Summarize these memories into a short digest:\n\n{recent}",
        ),
        ShellStep(
            name="save",
            command="cat > digest.md",
        ),
    ],
)
