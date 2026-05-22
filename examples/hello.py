"""Smallest possible workflow — two shell steps, no external services.

The first step produces a value; the second reads it through a
``{step_name}`` placeholder. Run it straight after install to confirm
The Brain works, before wiring up Memory Vault or an LLM.

Run it with:  brain run examples/hello.py
"""

from src.workflow import ShellStep, Workflow

workflow = Workflow(
    name="hello",
    steps=[
        ShellStep(
            name="greeting",
            command="echo 'Hello from The Brain'",
        ),
        ShellStep(
            name="echo_it_back",
            command="echo 'The previous step said: {greeting}'",
        ),
    ],
)
