"""Convention selection + token budget enforcement (GUR-105, P3A-1).

Responsibilities:
- Query active conventions from DirectivesRepository
- Sort by frequency descending (highest-impact first)
- Apply token budget truncation (default ≤ 2000 tokens)
- Return a list of Convention DTOs ready for adapter formatting

Token estimation:
    Uses chars/4 as a conservative heuristic. Real tokenizer overhead
    is unjustified here: the budget is a capacity guard, not a billing
    meter. Over-estimating (fewer conventions injected) is strictly
    safer than under-estimating (blowing past the budget). Claude's
    tokenizer averages ~3.5 chars/token for English; 4 gives margin.

Silent failure conditions:
    - If DirectivesRepository returns conventions with empty instruction
      fields, they consume 0 budget but produce empty injection lines.
      The adapter's inject_convention() handles this (returns "").
    - If ALL conventions exceed the budget individually, none are selected.
      This is correct: a single 3000-token convention should not be
      force-injected at 150% budget.

Design assumptions:
    - Convention ordering is deterministic: frequency DESC, then id ASC
      as tie-breaker. Same input → same output across restarts.
    - The token budget applies to the raw instruction text, not the
      formatted output. Adapter formatting overhead (headers, bullets)
      is bounded (~50 tokens) and absorbed by the conservative 4x divisor.

Ref: SD §5.8.3, §6.3
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from secondsight.analysis.schemas import Directive
    from secondsight.storage.directives_repository import DirectivesRepository

DEFAULT_TOKEN_BUDGET = 2000
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Convention:
    """Adapter-facing DTO for a convention selected for injection.

    Intentionally minimal: only fields the adapter needs to format the
    system prompt snippet. The adapter MUST NOT reach back into storage.
    """

    id: str
    instruction: str
    frequency: float | None
    source_flag_type: str | None


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate: ceil(len / CHARS_PER_TOKEN)."""
    return -(-len(text) // _CHARS_PER_TOKEN)


def _directive_to_convention(directive: "Directive") -> Convention:
    return Convention(
        id=directive.id,
        instruction=directive.instruction,
        frequency=directive.frequency,
        source_flag_type=directive.source_flag_type,
    )


class ConventionSelector:
    """Selects conventions within token budget for a project.

    Usage:
        selector = ConventionSelector(repo, token_budget=2000)
        conventions = selector.select(project_id)
        # conventions is List[Convention], total tokens ≤ budget
    """

    def __init__(
        self,
        repo: "DirectivesRepository",
        *,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> None:
        if token_budget <= 0:
            raise ValueError(f"token_budget must be positive, got {token_budget}")
        self._repo = repo
        self._token_budget = token_budget

    @property
    def token_budget(self) -> int:
        return self._token_budget

    def select(self, project_id: str) -> list[Convention]:
        """Select conventions for injection, respecting token budget.

        Returns conventions sorted by frequency DESC (greedy fill).
        Each convention's instruction is measured; conventions that would
        exceed remaining budget are skipped (not truncated mid-text).
        """
        directives = self._repo.get_active_conventions(project_id)

        selected: list[Convention] = []
        remaining_budget = self._token_budget

        for directive in directives:
            if not directive.instruction:
                continue
            cost = _estimate_tokens(directive.instruction)
            if cost > remaining_budget:
                continue
            selected.append(_directive_to_convention(directive))
            remaining_budget -= cost

        return selected
