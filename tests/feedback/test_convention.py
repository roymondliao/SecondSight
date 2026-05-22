"""Death tests for convention selection + budget enforcement (GUR-105, P3A-1).

Death cases:
- DT-1: Budget overflow — conventions exceeding remaining budget are SKIPPED,
  never truncated mid-text. Total tokens of selected conventions ≤ budget.
- DT-2: Empty instruction — conventions with empty instruction are never
  selected (would inject blank lines into system prompt).
- DT-3: Zero budget — raises ValueError at construction time.
- DT-4: Ordering determinism — same input produces same output (frequency
  DESC, then id ASC tie-break from the repo query).
- DT-5: Budget exactly exhausted — a convention whose cost equals remaining
  budget IS included (≤ not <).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.feedback.convention import (
    Convention,
    ConventionSelector,
    _estimate_tokens,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)


def _make_directive(
    *,
    id: str = "d1",
    instruction: str = "Do X not Y",
    frequency: float = 0.8,
    source_flag_type: str = "unnecessary_read",
    weight: float = 0.7,
) -> Directive:
    return Directive(
        id=id,
        project_id="proj-1",
        type=DirectiveType.CONVENTION,
        status=DirectiveStatus.ACTIVE,
        instruction=instruction,
        frequency=frequency,
        source_flag_type=source_flag_type,
        identity_key=f"key-{id}",
        weight=weight,
        source_sessions=["s1"],
        created_at=NOW,
        updated_at=NOW,
    )


def _mock_repo(directives: list[Directive]) -> MagicMock:
    repo = MagicMock()
    repo.get_active_conventions.return_value = directives
    return repo


class TestDeathPaths:
    def test_dt_1_budget_overflow_skips_not_truncates(self) -> None:
        """DT-1: A convention exceeding remaining budget is skipped entirely."""
        small = _make_directive(id="small", instruction="A" * 20, frequency=0.9)
        large = _make_directive(id="large", instruction="B" * 100, frequency=0.8)
        repo = _mock_repo([small, large])

        selector = ConventionSelector(repo, token_budget=10)
        result = selector.select("proj-1")

        total_tokens = sum(_estimate_tokens(c.instruction) for c in result)
        assert total_tokens <= 10
        assert all(c.id != "large" for c in result)

    def test_dt_2_empty_instruction_never_selected(self) -> None:
        """DT-2: Convention with empty instruction is silently excluded."""
        empty = _make_directive(id="empty", instruction="", frequency=0.99)
        normal = _make_directive(id="normal", instruction="Read AGENTS.md", frequency=0.5)
        repo = _mock_repo([empty, normal])

        selector = ConventionSelector(repo, token_budget=2000)
        result = selector.select("proj-1")

        assert len(result) == 1
        assert result[0].id == "normal"

    def test_dt_3_zero_budget_raises(self) -> None:
        """DT-3: Zero or negative budget is rejected at construction."""
        repo = _mock_repo([])
        with pytest.raises(ValueError, match="positive"):
            ConventionSelector(repo, token_budget=0)
        with pytest.raises(ValueError, match="positive"):
            ConventionSelector(repo, token_budget=-100)

    def test_dt_4_ordering_deterministic(self) -> None:
        """DT-4: Same input always produces same selection order."""
        d1 = _make_directive(id="a", instruction="First", frequency=0.9)
        d2 = _make_directive(id="b", instruction="Second", frequency=0.5)
        repo = _mock_repo([d1, d2])

        selector = ConventionSelector(repo, token_budget=2000)
        r1 = selector.select("proj-1")
        r2 = selector.select("proj-1")

        assert [c.id for c in r1] == [c.id for c in r2]
        assert r1[0].id == "a"

    def test_dt_4_b_weight_does_not_affect_selection_order(self) -> None:
        """Ordering remains repo/frequency-based even when lifecycle weight exists."""
        d1 = _make_directive(id="freq-high", instruction="First", frequency=0.9, weight=0.1)
        d2 = _make_directive(id="freq-low", instruction="Second", frequency=0.5, weight=0.95)
        repo = _mock_repo([d1, d2])

        selector = ConventionSelector(repo, token_budget=2000)
        result = selector.select("proj-1")

        assert [c.id for c in result] == ["freq-high", "freq-low"]

    def test_dt_5_budget_exactly_exhausted_includes(self) -> None:
        """DT-5: Convention whose cost == remaining budget is included."""
        instruction = "X" * 40
        cost = _estimate_tokens(instruction)
        d = _make_directive(id="exact", instruction=instruction, frequency=0.9)
        repo = _mock_repo([d])

        selector = ConventionSelector(repo, token_budget=cost)
        result = selector.select("proj-1")

        assert len(result) == 1
        assert result[0].id == "exact"


class TestHappyPath:
    def test_no_conventions_returns_empty(self) -> None:
        repo = _mock_repo([])
        selector = ConventionSelector(repo, token_budget=2000)
        assert selector.select("proj-1") == []

    def test_multiple_within_budget(self) -> None:
        d1 = _make_directive(id="a", instruction="Short rule", frequency=0.9)
        d2 = _make_directive(id="b", instruction="Another rule", frequency=0.7)
        repo = _mock_repo([d1, d2])

        selector = ConventionSelector(repo, token_budget=2000)
        result = selector.select("proj-1")

        assert len(result) == 2
        assert isinstance(result[0], Convention)

    def test_token_estimate_conservative(self) -> None:
        assert _estimate_tokens("hello") == 2  # 5 chars / 4 = 1.25 → ceil = 2
        assert _estimate_tokens("a" * 8) == 2  # 8 / 4 = 2
        assert _estimate_tokens("") == 0
