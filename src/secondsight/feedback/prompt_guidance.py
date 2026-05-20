"""UserPromptSubmit bypass matching and guidance template loading."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from re import Pattern

from secondsight.prompts._loader import render


class PromptGuidanceError(ValueError):
    """Raised when prompt guidance cannot be mapped from a classifier category."""


class PromptHitCategory(StrEnum):
    """Closed v1 categories returned by the ambiguity evaluator."""

    MISSING_TARGET = "missing_target"
    MULTIPLE_INTERPRETATIONS = "multiple_interpretations"
    MISSING_SCOPE = "missing_scope"
    MISSING_SUCCESS_CRITERIA = "missing_success_criteria"


HIT_GUIDANCE_TEMPLATE_NAMES: dict[PromptHitCategory, str] = {
    PromptHitCategory.MISSING_TARGET: "feedback/guidance/missing_target",
    PromptHitCategory.MULTIPLE_INTERPRETATIONS: "feedback/guidance/multiple_interpretations",
    PromptHitCategory.MISSING_SCOPE: "feedback/guidance/missing_scope",
    PromptHitCategory.MISSING_SUCCESS_CRITERIA: "feedback/guidance/missing_success_criteria",
}


def guidance_for_category(category: PromptHitCategory | str) -> str:
    """Return the fixed v1 guidance template for a hit category."""
    try:
        normalized = PromptHitCategory(category)
    except ValueError as exc:
        raise PromptGuidanceError(f"Unknown prompt hit category: {category!r}") from exc
    try:
        template_name = HIT_GUIDANCE_TEMPLATE_NAMES[normalized]
    except KeyError as exc:
        raise PromptGuidanceError(f"No guidance template registered for: {normalized!r}") from exc
    return render(template_name, context={}).strip()


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
    "HIT_GUIDANCE_TEMPLATE_NAMES",
    "PromptGuidanceError",
    "PromptHitCategory",
    "bypass_registry",
    "guidance_for_category",
]
