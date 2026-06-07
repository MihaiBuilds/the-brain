"""Example workflow — process an incoming webhook payload.

Reads the inbound body via the ``{trigger.body}`` placeholder and
writes a one-line summary to a file. Hermetic: no Memory Vault
dependency, no LLM dependency, just a ShellStep that echoes the
trigger fields.

Register it as a webhook trigger:

    brain register-webhook examples/webhook_handler.py

The CLI prints the HMAC secret once. Sign the request body with
HMAC-SHA256 and send the digest in the ``X-Brain-Signature`` header
as ``sha256=<hex>``. Bring up the API profile to expose the endpoint:

    THE_BRAIN_API_TOKEN=any-value docker compose --profile api up -d

Then POST a payload (signed with the secret you saved):

    SECRET=<your-secret>
    BODY='{"hello": "world"}'
    SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
    curl -X POST http://localhost:8001/webhook/webhook-handler \\
        -H "X-Brain-Signature: $SIG" \\
        -H "Content-Type: application/json" \\
        -d "$BODY"

The endpoint runs the workflow synchronously and returns the run
metadata. ``brain history`` shows the run with its trigger context
preserved in the workflow_runs row.
"""

from src.workflow import ShellStep, Workflow

workflow = Workflow(
    name="webhook-handler",
    steps=[
        ShellStep(
            name="received",
            command="echo got event={trigger.event} body={trigger.body}",
        ),
        ShellStep(
            name="record",
            command="echo {received} >> /tmp/webhook-log.txt",
        ),
    ],
)
