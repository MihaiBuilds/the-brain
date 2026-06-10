"""
Workflow file format — the Pydantic models users author.

A workflow file is a plain Python file that defines a module-level
``workflow`` variable:

    from src.workflow import Workflow, MemoryVaultStep, LLMStep, ShellStep

    workflow = Workflow(
        name="daily-digest",
        steps=[
            MemoryVaultStep(name="recent", query="this week", space="work"),
            LLMStep(name="summarize", prompt="Summarize: {recent}"),
            ShellStep(name="save", command="cat > digest.md"),
        ],
    )

Each step type maps to one integration. Step ``config`` is validated at
authoring time by these models — a typo fails when the file is loaded,
not mid-run. A ``{step_name}`` placeholder in a field is stored verbatim
here; the runner substitutes prior step output.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class _StepBase(BaseModel):
    """Fields common to every step. Not used directly — see the subclasses."""

    name: str = Field(min_length=1, description="Unique label within the workflow.")

    model_config = {"extra": "forbid"}


class MemoryVaultStep(_StepBase):
    """Query Memory Vault over its REST API."""

    type: Literal["memory_vault"] = "memory_vault"
    query: str = Field(min_length=1)
    space: str | None = Field(default=None, description="Restrict to one memory space.")
    limit: int = Field(default=10, ge=1, le=100)


class LLMStep(_StepBase):
    """Call an OpenAI-compatible LLM endpoint.

    Tested against LM Studio only. Other OpenAI-compatible providers
    (Ollama, vLLM, llama.cpp server, OpenAI proper) may work via the
    same wire format but are not promised in v1.0.

    Optional per-step fields override config defaults from settings;
    leave them as None to fall back to ``LLM_BASE_URL``, ``LLM_API_KEY``,
    and the default 120s timeout.
    """

    type: Literal["llm"] = "llm"
    prompt: str = Field(min_length=1)
    system: str | None = None
    model: str | None = Field(default=None, description="Override LLM_MODEL.")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    provider_url: str | None = Field(default=None, description="Override LLM_BASE_URL.")
    api_key: str | None = Field(default=None, description="Override LLM_API_KEY.")
    timeout_seconds: float | None = Field(default=None, gt=0.0, description="Override the default 120s timeout.")
    max_tokens: int | None = Field(default=None, ge=1, description="Cap response length.")


class ShellStep(_StepBase):
    """Run a shell command as a subprocess."""

    type: Literal["shell"] = "shell"
    command: str = Field(min_length=1)
    timeout: int = Field(default=60, ge=1, le=3600, description="Seconds before kill.")


# Discriminated union — Pydantic picks the subclass from the ``type`` field.
Step = Annotated[
    MemoryVaultStep | LLMStep | ShellStep,
    Field(discriminator="type"),
]


class Workflow(BaseModel):
    """An ordered list of steps, run top to bottom."""

    name: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def _name_no_whitespace(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("workflow name must not have leading/trailing whitespace")
        return v

    @model_validator(mode="after")
    def _step_names_unique(self) -> "Workflow":
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise ValueError(f"duplicate step name: {step.name!r}")
            seen.add(step.name)
        return self
