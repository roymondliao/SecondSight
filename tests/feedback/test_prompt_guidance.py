"""Prompt guidance bypass and fixed-template contract tests.

Death tests come first: bypass must be explicit and agent-scoped so a prompt
that should never be classified does not silently spend latency or recurse.
"""

from __future__ import annotations

import pytest

from secondsight.feedback.prompt_guidance import (
    HIT_GUIDANCE_TEMPLATES,
    PromptHitCategory,
    PromptGuidanceError,
    bypass_registry,
    guidance_for_category,
)


# ===========================================================================
# DEATH TESTS
# ===========================================================================


def test_dt_bypass_registry_detects_slash_command_without_classification() -> None:
    """Slash commands are agent-native control flow, not ambiguous user intent."""
    assert bypass_registry.should_bypass(agent="claude_code", prompt="/compact now")
    assert bypass_registry.should_bypass(agent="codex", prompt="   /review")


def test_dt_bypass_registry_is_agent_scoped_not_global() -> None:
    """A bypass for supported agents must not silently apply to unknown agents."""
    assert not bypass_registry.should_bypass(agent="unknown_agent", prompt="/compact now")


# ===========================================================================
# UNIT TESTS
# ===========================================================================


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (
            PromptHitCategory.MISSING_TARGET,
            "Clarify which file, module, error, or workflow this request refers to before acting.",
        ),
        (
            PromptHitCategory.MULTIPLE_INTERPRETATIONS,
            "Clarify the intended outcome or approach before acting, since this request could be interpreted in multiple valid ways.",
        ),
        (
            PromptHitCategory.MISSING_SCOPE,
            "Clarify the intended scope before acting, such as analysis only, code changes, tests, or refactoring.",
        ),
        (
            PromptHitCategory.MISSING_SUCCESS_CRITERIA,
            "Clarify what outcome should count as complete before acting, including how success should be verified.",
        ),
    ],
)
def test_guidance_for_category_returns_fixed_template(
    category: PromptHitCategory,
    expected: str,
) -> None:
    assert guidance_for_category(category) == expected
    assert HIT_GUIDANCE_TEMPLATES[category] == expected


def test_guidance_for_category_rejects_unknown_category() -> None:
    with pytest.raises(PromptGuidanceError):
        guidance_for_category("not-a-category")  # type: ignore[arg-type]
