"""Tests for the M4 example file + README documentation surface.

The example file must be importable (workflow-loader runs the file as
Python). README sections that document locked design behaviors are
pinned by grep tests so future refactors can't silently drop the
caveats.
"""

from __future__ import annotations

from pathlib import Path

from src.workflow import McpToolStep, Workflow, import_workflow_from_file

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Example file
# ---------------------------------------------------------------------------


def test_mcp_recall_memory_example_imports_cleanly():
    path = REPO_ROOT / "examples" / "mcp_recall_memory.py"
    workflow = import_workflow_from_file(path)
    assert isinstance(workflow, Workflow)
    assert workflow.name == "mcp-recall-memory"


def test_mcp_recall_memory_example_uses_mcp_tool_step():
    path = REPO_ROOT / "examples" / "mcp_recall_memory.py"
    workflow = import_workflow_from_file(path)
    mcp_steps = [s for s in workflow.steps if isinstance(s, McpToolStep)]
    assert len(mcp_steps) == 1
    assert mcp_steps[0].tool == "recall"


def test_mcp_recall_memory_example_chains_via_previous_x():
    # The example demonstrates the {previous.X} integration story by
    # piping the MCP step's output into a downstream LLMStep prompt.
    # If that wiring breaks, the example stops being a useful demo.
    path = REPO_ROOT / "examples" / "mcp_recall_memory.py"
    text = path.read_text(encoding="utf-8")
    assert "{recall}" in text


# ---------------------------------------------------------------------------
# README documentation pins
# ---------------------------------------------------------------------------


def _readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_documents_mcp_tool_section():
    text = _readme_text()
    assert "## Call an MCP tool from a workflow" in text
    assert "McpToolStep" in text


def test_readme_pins_substitution_boundary_caveats():
    # The locked rules: tool name + args keys are NOT substituted.
    # Drop them from the public docs and a downstream PR could silently
    # change the contract — this test catches that.
    text = _readme_text()
    assert "tool` name" in text
    assert "args` keys" in text


def test_readme_documents_derive_your_own_image_pattern():
    # The ecosystem-rule lock: stock image bundles ZERO MCP servers.
    # Both the lock and the pattern must be public.
    text = _readme_text()
    assert "derive-your-own-image" in text or "derive your own image" in text
    assert "FROM mihaibuilds/the-brain" in text
    assert "bundles ZERO MCP servers" in text


def test_readme_documents_memory_vault_step_vs_mcp_tool_step_choice():
    # The locked coexistence policy must be discoverable in the README
    # — without it, users will assume one path deprecates the other.
    text = _readme_text()
    assert "MemoryVaultStep` vs `McpToolStep" in text
    assert "REST" in text  # the table mentions REST vs MCP transports
