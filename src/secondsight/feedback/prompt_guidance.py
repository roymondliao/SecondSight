"""UserPromptSubmit bypass matching and fixed guidance templates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from re import Pattern


class PromptGuidanceError(ValueError):
    """Raised when prompt guidance cannot be mapped from a classifier category."""


class PromptHitCategory(StrEnum):
    """Closed v1 categories returned by the ambiguity evaluator."""

    MISSING_TARGET = "missing_target"
    MULTIPLE_INTERPRETATIONS = "multiple_interpretations"
    MISSING_SCOPE = "missing_scope"
    MISSING_SUCCESS_CRITERIA = "missing_success_criteria"


HIT_GUIDANCE_TEMPLATES: dict[PromptHitCategory, str] = {
    PromptHitCategory.MISSING_TARGET: (
        "Clarify which file, module, error, or workflow this request refers to before acting."
    ),
    PromptHitCategory.MULTIPLE_INTERPRETATIONS: (
        "Clarify the intended outcome or approach before acting, since this request could be "
        "interpreted in multiple valid ways."
    ),
    PromptHitCategory.MISSING_SCOPE: (
        "Clarify the intended scope before acting, such as analysis only, code changes, "
        "tests, or refactoring."
    ),
    PromptHitCategory.MISSING_SUCCESS_CRITERIA: (
        "Clarify what outcome should count as complete before acting, including how success "
        "should be verified."
    ),
}


def guidance_for_category(category: PromptHitCategory | str) -> str:
    """Return the fixed v1 guidance template for a hit category."""
    try:
        normalized = PromptHitCategory(category)
    except ValueError as exc:
        raise PromptGuidanceError(f"Unknown prompt hit category: {category!r}") from exc
    try:
        return HIT_GUIDANCE_TEMPLATES[normalized]
    except KeyError as exc:
        raise PromptGuidanceError(f"No guidance template registered for: {normalized!r}") from exc


@dataclass(frozen=True)
class AgentBypassRegistry:
    """Agent-scoped bypass pattern registry.

    Bypass prompts are agent-native control flow, not user intent requiring an
    LLM classification hop. Patterns are scoped by agent so adding a bypass for
    one agent cannot silently disable guidance for every future adapter.
    """

    patterns_by_agent: dict[str, tuple[Pattern[str], ...]]

    def should_bypass(self, *, agent: str, prompt: str) -> bool:
        patterns = self.patterns_by_agent.get(agent, ())
        return any(pattern.search(prompt) is not None for pattern in patterns)


def _compile(pattern: str) -> Pattern[str]:
    return re.compile(pattern, flags=re.IGNORECASE)


bypass_registry = AgentBypassRegistry(
    patterns_by_agent={
        "claude_code": (
            _compile(r"^\s*/"),
            _compile(r"^\s*(?:secondsight:)?\s*(?:bypass|no[-_ ]?guidance)\b"),
        ),
        "codex": (
            _compile(r"^\s*/"),
            _compile(r"^\s*(?:secondsight:)?\s*(?:bypass|no[-_ ]?guidance)\b"),
        ),
    }
)


__all__ = [
    "AgentBypassRegistry",
    "HIT_GUIDANCE_TEMPLATES",
    "PromptGuidanceError",
    "PromptHitCategory",
    "bypass_registry",
    "guidance_for_category",
]
