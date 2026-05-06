"""Death + coverage tests for analysis.prompts.aggregate (GUR-101 P2-6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secondsight.analysis.prompts.aggregate import (
    AggregateOutput,
    AggregatePattern,
    FlagSummary,
    build_aggregate_prompt,
)
from secondsight.analysis.schemas import BehaviorFlagType


def _flag_summary(**overrides: object) -> FlagSummary:
    base: dict[str, object] = {
        "session_id": "sess-1",
        "segment_summary": "agent read README before editing target file",
        "reason": "README unrelated to the bug fix",
    }
    base.update(overrides)
    return FlagSummary(**base)


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_a1_rejects_string_flag_type(self) -> None:
        """DT-A1 — passing a raw string would silently allow drift
        from the SD vocabulary. The builder rejects with TypeError.
        """
        with pytest.raises(TypeError):
            build_aggregate_prompt("unnecessary_read", [])  # type: ignore[arg-type]

    def test_dt_a2_aggregate_pattern_rejects_zero_occurrence_count(
        self,
    ) -> None:
        """DT-A2 — a pattern with zero occurrences is incoherent and
        would survive a permissive parser. Pydantic validation
        rejects it explicitly.
        """
        with pytest.raises(ValidationError):
            AggregatePattern(
                pattern_description="x",
                occurrence_count=0,
                representative_sessions=[],
                convention="y",
            )

    def test_dt_a3_aggregate_pattern_rejects_negative_occurrence_count(
        self,
    ) -> None:
        with pytest.raises(ValidationError):
            AggregatePattern(
                pattern_description="x",
                occurrence_count=-3,
                representative_sessions=[],
                convention="y",
            )

    def test_dt_a4_flag_summary_rejects_extra_fields(self) -> None:
        """DT-A4 — extra='forbid' on FlagSummary catches accidental
        field-leak from BehaviorFlag (e.g., id / project_id) which
        would inflate prompt input tokens.
        """
        with pytest.raises(ValidationError):
            FlagSummary.model_validate(
                {
                    "session_id": "s",
                    "segment_summary": "x",
                    "reason": "y",
                    "id": "leaked",
                }
            )


# =====================================================================
# COVERAGE TESTS
# =====================================================================


class TestBuildAggregatePrompt:
    def test_flag_type_value_appears_in_prompt(self) -> None:
        prompt = build_aggregate_prompt(BehaviorFlagType.MISSED_SHORTCUT, [])
        assert "missed_shortcut" in prompt

    def test_flag_type_description_appears_in_prompt(self) -> None:
        """Coverage — the SD §5.5.1 description for the flag type
        appears in the prompt, giving the LLM a definitional anchor
        rather than just a label.
        """
        prompt = build_aggregate_prompt(BehaviorFlagType.MISSED_SHORTCUT, [])
        assert "有更直接的路徑可達成目標但沒走" in prompt

    def test_empty_flags_renders_valid_prompt(self) -> None:
        """Coverage — empty input does not raise; LLM will produce
        patterns=[] which AggregateOutput accepts.
        """
        prompt = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, [])
        assert "[Behavior Flags]" in prompt
        assert "[]" in prompt

    def test_pure_function_determinism(self) -> None:
        flags = [_flag_summary(), _flag_summary(session_id="sess-2")]
        a = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, flags)
        b = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, flags)
        assert a == b

    def test_flag_summary_payload_passes_through(self) -> None:
        prompt = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, [_flag_summary()])
        assert "sess-1" in prompt
        assert "agent read README before editing target file" in prompt
        assert "README unrelated to the bug fix" in prompt

    def test_convention_spec_in_task_block(self) -> None:
        prompt = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, [])
        assert "≤ 200 tokens" in prompt
        assert "可操作的指導" in prompt


class TestAggregateOutput:
    def test_accepts_well_formed_sd_example(self) -> None:
        payload = {
            "patterns": [
                {
                    "pattern_description": "reads README before editing",
                    "occurrence_count": 12,
                    "representative_sessions": ["s-a", "s-b"],
                    "convention": (
                        "When the user names a target file, edit it "
                        "directly without scanning README or unrelated "
                        "files first."
                    ),
                }
            ]
        }
        parsed = AggregateOutput.model_validate(payload)
        assert len(parsed.patterns) == 1
        assert parsed.patterns[0].occurrence_count == 12

    def test_accepts_empty_patterns(self) -> None:
        AggregateOutput.model_validate({"patterns": []})
