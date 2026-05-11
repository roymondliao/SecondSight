"""Tests for hint module interface (GUR-108, P3B-4)."""

from __future__ import annotations

from secondsight.feedback.hint import Hint, HintSelector


class TestHint:
    def test_hint_creation(self) -> None:
        hint = Hint(
            id="h-1",
            instruction="Check for unused imports",
            trigger_pattern="*.py",
            confidence=0.9,
        )
        assert hint.id == "h-1"
        assert hint.instruction == "Check for unused imports"

    def test_hint_is_frozen(self) -> None:
        hint = Hint(id="h-1", instruction="test", trigger_pattern="*", confidence=0.5)
        try:
            hint.id = "h-2"  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass


class TestHintSelector:
    def test_match_returns_empty_list(self) -> None:
        selector = HintSelector()
        result = selector.match("proj-1", {"events": []})
        assert result == []

    def test_inject_returns_empty_string(self) -> None:
        selector = HintSelector()
        hint = Hint(id="h-1", instruction="test", trigger_pattern="*", confidence=0.5)
        result = selector.inject(hint)
        assert result == ""
