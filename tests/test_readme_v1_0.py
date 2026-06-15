"""Tests locking the v1.0 README structure.

The README's v1.0 shape mirrors Memory Vault's released README for ecosystem
consistency: same Status / Limitations / FAQ / PRO-tier section pattern, with
Brain-specific content swapped in. These tests lock the new sections by
heading so a future refactor can't quietly drop one.

They also lock the deliberate OMISSIONS — the old ``## Roadmap`` table was
removed in favor of a ``## Status`` block (matching MV), and a ``## Credits``
section is intentionally NOT shipped at v1.0 because there are no peer-builder
credits yet to populate it. Both omissions are tested so future drift can't
silently re-add them by cargo-culting MV's template.

Tests in this file complement (don't replace) ``tests/test_m4_docs_and_example.py``,
which locks M4-specific README content (MemoryVaultStep vs McpToolStep,
derive-your-own-image, substitution boundaries).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# v1.0 sections present (heading-level locks)
# ---------------------------------------------------------------------------


def test_readme_has_status_section():
    """The ``## Status`` section replaces the build-in-public roadmap table,
    matching MV's released README pattern."""
    text = _readme_text()
    assert "## Status" in text
    # Status section names v1.0 explicitly so evaluators see the contract.
    assert "v1.0" in text


def test_readme_status_has_release_date_placeholder_or_real_date():
    """Until release tag lands, the placeholder is ``YYYY-MM-DD``. When the
    release ships, that placeholder is replaced with the real ISO date.
    This test accepts either."""
    text = _readme_text()
    # Real ISO date pattern: 4 digits, hyphen, 2 digits, hyphen, 2 digits.
    # Placeholder pattern: literal ``YYYY-MM-DD``.
    import re

    pattern = re.compile(r"released (\d{4}-\d{2}-\d{2}|YYYY-MM-DD)")
    assert pattern.search(text) is not None, (
        "Status section must contain 'released YYYY-MM-DD' placeholder or "
        "a real ISO date (e.g. 'released 2026-07-01')."
    )


def test_readme_has_workflow_lifecycle_hooks_section():
    """The named-hooks/extension-points section is Brain-specific and exposes
    the inspectable workflow surface for evaluators reading the repo cold."""
    text = _readme_text()
    assert "## Workflow lifecycle hooks" in text
    # The section must reference the locked substitution boundaries so a
    # future rewrite cannot drop the contract.
    assert "{previous.X}" in text
    assert "{trigger.X}" in text
    assert "isError" in text


def test_readme_has_limitations_section():
    """MV-parity ``## Limitations`` section with honest v1.0 limits."""
    text = _readme_text()
    assert "## Limitations" in text
    # The section must name the LM Studio-only caveat as part of the honest
    # limits framing, not just in the step types table.
    assert "LM Studio" in text


def test_readme_has_pro_tier_section():
    """``## PRO tier (planned)`` section — MV-parity open-core framing."""
    text = _readme_text()
    assert "## PRO tier (planned)" in text
    # Open-core promise is the load-bearing claim of this section.
    assert "open-core" in text.lower()


def test_readme_has_faq_section():
    """``## FAQ`` section with evaluator-conversion-shaped questions."""
    text = _readme_text()
    assert "## FAQ" in text
    # The "how is this different from..." question is the highest-value FAQ
    # for any developer evaluating a workflow orchestrator.
    assert "n8n" in text
    assert "Temporal" in text
    assert "Airflow" in text


# ---------------------------------------------------------------------------
# Hero region locks (the 4 trigger types + 4 step types must be named upfront)
# ---------------------------------------------------------------------------


def test_readme_hero_names_four_trigger_types():
    """The hero must name all four trigger types so a 30-second skim shows
    the surface immediately, matching MV's hero shape."""
    text = _readme_text()
    # Search the top ~80 lines (the hero + Status region).
    hero_region = "\n".join(text.splitlines()[:80])
    assert "manual" in hero_region.lower()
    assert "cron" in hero_region.lower()
    assert "webhook" in hero_region.lower()
    assert "file" in hero_region.lower()


def test_readme_hero_names_four_step_types():
    """The hero must name all four step types so a 30-second skim shows
    what The Brain can do."""
    text = _readme_text()
    hero_region = "\n".join(text.splitlines()[:80])
    assert "shell" in hero_region.lower()
    assert "llm" in hero_region.lower()
    assert "memory vault" in hero_region.lower()
    assert "mcp" in hero_region.lower()


# ---------------------------------------------------------------------------
# MV-parity sections added/reordered in the post-#41 corrective pass
# ---------------------------------------------------------------------------


def test_readme_has_no_docker_quick_start_section():
    """The Python-from-source install path lives in its own section, mirroring
    MV. Non-Docker users shouldn't have to read the Docker walkthrough first."""
    text = _readme_text()
    assert "## No-Docker quick start" in text
    # Must reference the pip-install + brain migrate path to be useful.
    assert "pip install -e ." in text
    assert "brain migrate" in text


def test_readme_has_features_section_not_what_v1_0_does():
    """The MV-shape ``## Features`` section replaces the pre-v1.0
    ``## What v1.0 does`` heading. Locks the rename so future drift
    can't revert to the build-in-public phrasing."""
    text = _readme_text()
    assert "## Features" in text
    # Match the heading as a whole line (\n on both sides) so the substring
    # doesn't false-positive on ``### What v1.0 doesn't ship``.
    assert "\n## What v1.0 does\n" not in text


def test_readme_has_architecture_section_with_three_deliberate_choices():
    """The ``## Architecture`` section mirrors MV's "three deliberate things"
    pattern. Names the three locked choices so an evaluator sees the shape
    in 30 seconds without reading the deeper integration walkthroughs."""
    text = _readme_text()
    assert "## Architecture" in text
    # The three load-bearing claims of the architecture must be present.
    assert "One database" in text
    assert "Process boundary" in text
    assert "Per-step spawn" in text


def test_readme_has_tech_stack_section_not_tech():
    """The pre-v1.0 ``## Tech`` heading is renamed to ``## Tech Stack`` for
    MV-parity and moved to early position. Locks both the rename and the
    removal of the old heading."""
    text = _readme_text()
    assert "## Tech Stack" in text
    assert "## Tech\n" not in text  # standalone ``## Tech`` heading
    assert "\n## Tech\n" not in text


def test_readme_has_how_it_works_section():
    """The ``## How It Works`` section names the workflow execution model,
    the four trigger surfaces, and the substitution boundaries. Brain-specific
    content mirroring MV's section shape."""
    text = _readme_text()
    assert "## How It Works" in text
    # The three locked sub-headings of How It Works.
    assert "### Workflow execution" in text
    assert "### Trigger surfaces" in text
    assert "### Substitution model" in text


def test_readme_has_troubleshooting_section_referencing_brain_diagnose():
    """The ``## Troubleshooting`` section documents the ``brain diagnose``
    command and warns users to review the bundle before posting to a
    public issue tracker. Locks both the section presence and the
    review-before-post discipline."""
    text = _readme_text()
    assert "## Troubleshooting" in text
    assert "brain diagnose" in text
    # The defense-in-depth warning about reviewing the bundle MUST be in
    # the troubleshooting section — secret hygiene is part of the contract.
    assert "Review every file" in text


def test_readme_has_follow_the_build_section():
    """The ``## Follow the Build`` footer matches MV. Final section, with
    links to website / blog / GitHub / X. Locks the v1.0 footer so future
    drift doesn't silently drop the call-to-action."""
    text = _readme_text()
    assert "## Follow the Build" in text
    assert "mihaibuilds.com" in text
    # X handle reference (without the @ which can drift to "at-")
    assert "x.com/mihaibuilds" in text


def test_readme_does_not_have_what_v1_0_doesnt_do_section():
    """The pre-v1.0 ``## What v1.0 doesn't do`` section was merged into
    ``## Limitations`` as a ``### What v1.0 doesn't ship`` sub-group.
    One section, two grouped lists — kills the redundancy that had two
    headings doing the same job."""
    text = _readme_text()
    # Whole-line match so we don't false-positive on the new
    # ``### What v1.0 doesn't ship`` sub-heading.
    assert "\n## What v1.0 doesn't do\n" not in text
    # The content moved into Limitations as a sub-heading.
    assert "### What v1.0 doesn't ship" in text


def test_readme_section_order_matches_mv_parity():
    """Lock the v1.0 section order against future drift. Catches the case
    where someone reorders sections without thinking — section ORDER is
    part of the ecosystem-parity contract, not just section EXISTENCE."""
    text = _readme_text()
    import re

    headings = re.findall(r"^## .+$", text, flags=re.MULTILINE)

    expected = [
        "## Status",
        "## Quick Start (Docker)",
        "## No-Docker quick start",
        "## Features",
        "## Architecture",
        "## Tech Stack",
        "## How It Works",
        "## Run workflows on a schedule",
        "## React to webhooks",
        "## React to file changes",
        "## Call an MCP tool from a workflow",
        "## Trigger types",
        "## Workflow lifecycle hooks",
        "## Troubleshooting",
        "## Limitations",
        "## PRO tier (planned)",
        "## FAQ",
        "## License",
        "## Follow the Build",
    ]

    assert headings == expected, (
        f"README section order drifted from MV-parity.\n"
        f"Expected ({len(expected)} sections):\n  "
        + "\n  ".join(expected)
        + f"\nActual ({len(headings)} sections):\n  "
        + "\n  ".join(headings)
    )


# ---------------------------------------------------------------------------
# Deliberate omissions — locked so future drift can't quietly re-add them
# ---------------------------------------------------------------------------


def test_readme_does_not_have_roadmap_section():
    """The pre-v1.0 ``## Roadmap`` table was removed in favor of ``## Status``.
    Matches MV's released README pattern — build-history goes to blog + release
    notes, not into the README of a shipped product."""
    text = _readme_text()
    assert "## Roadmap" not in text


def test_readme_does_not_have_credits_section():
    """v1.0 ships without a ``## Credits`` section by design — there are no
    peer-builder credits to populate it yet. A future PR can add it when
    real engagement lands. This test locks the omission so cargo-culting
    MV's template doesn't silently add empty/padded credits."""
    text = _readme_text()
    assert "## Credits" not in text
