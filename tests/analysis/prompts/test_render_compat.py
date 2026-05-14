"""Regression tests: rendered jinja2 templates vs. old Python string constants.

These tests verify that the refactored render functions (which call the
jinja2 loader under the hood) produce output equivalent to what the old
Python constants produced when called with the same inputs.

"Equivalent" is defined as: rendered output for the same inputs matches
the old-constant-based output byte-for-byte OR the diff is captured and
documented in the scar report as an approved divergence (e.g., whitespace
normalization from trim_blocks=True).

If a test here fails after the refactor, the diff MUST be reviewed before
approving — a silent content drop (variable omitted, section removed) is
NOT acceptable divergence.
"""

from __future__ import annotations


import pytest

from secondsight.analysis.schemas import (
    BehaviorFlagDraft,
    BehaviorFlagType,
    SegmentAnalysis,
    SegmentData,
    SegmentMetrics,
)


# =====================================================================
# FIXTURES
# =====================================================================


def _segment(events: list = (), user_prompt=None) -> SegmentData:
    return SegmentData(
        segment_index=1,
        user_prompt=user_prompt,
        events=list(events),
        session_id="sess-compat-1",
        project_id="proj-compat-1",
    )


def _metrics() -> SegmentMetrics:
    return SegmentMetrics(total_tokens=1234, unique_files=2, duration=3.5, error_count=0)


def _segment_analysis(flagged: int = 1) -> SegmentAnalysis:
    return SegmentAnalysis(
        segment_summary="agent read one extra unrelated file",
        flags=[
            BehaviorFlagDraft(
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                event_ids=[f"e{i}"],
                reason="unrelated file",
                confidence="high",
            )
            for i in range(flagged)
        ],
        total_events=5,
        flagged_events=flagged,
    )


# =====================================================================
# BEHAVIOR PROMPT COMPAT
# =====================================================================


class TestBehaviorPromptCompat:
    """Verify refactored build_segment_prompt is backward-compatible."""

    def test_behavior_prompt_is_non_empty(self) -> None:
        """build_segment_prompt must produce non-empty output after
        the jinja2 refactor.
        """
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        result = build_segment_prompt(_segment(), _metrics())
        assert len(result) > 100

    def test_behavior_prompt_contains_all_flag_types(self) -> None:
        """All BehaviorFlagType members must appear in the rendered prompt.
        Regression: old prompt iterated the enum; new template must
        receive the flag_definitions_block with all six types.
        """
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        result = build_segment_prompt(_segment(), _metrics())
        for flag_type in BehaviorFlagType:
            assert flag_type.value in result, (
                f"flag_type {flag_type.value!r} missing from refactored behavior prompt"
            )

    def test_behavior_prompt_contains_confidence_field(self) -> None:
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        result = build_segment_prompt(_segment(), _metrics())
        assert '"confidence"' in result

    def test_behavior_prompt_user_prompt_null_for_pre_prompt_segment(self) -> None:
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        result = build_segment_prompt(_segment(user_prompt=None), _metrics())
        assert '"user_prompt": null' in result

    def test_behavior_prompt_metrics_pass_through(self) -> None:
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        m = SegmentMetrics(total_tokens=42, unique_files=7, duration=1.5, error_count=3)
        result = build_segment_prompt(_segment(), m)
        assert '"total_tokens": 42' in result
        assert '"unique_files": 7' in result
        assert '"error_count": 3' in result

    def test_behavior_prompt_determinism(self) -> None:
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        seg = _segment()
        m = _metrics()
        assert build_segment_prompt(seg, m) == build_segment_prompt(seg, m)

    def test_behavior_prompt_contains_analysis_output_schema(self) -> None:
        """New requirement: behavior prompt must embed AnalysisOutput schema.
        This is a forward addition (not in old constant); CLI mode agents
        need the output schema to produce conformant JSON.
        """
        from secondsight.analysis.prompts.behavior import build_segment_prompt

        result = build_segment_prompt(_segment(), _metrics())
        assert "schema_version" in result, (
            "AnalysisOutput schema missing from behavior prompt — "
            "CLI agents cannot produce conformant output"
        )


# =====================================================================
# SUMMARY PROMPT COMPAT
# =====================================================================


class TestSummaryPromptCompat:
    """Verify refactored build_summary_prompt is backward-compatible."""

    def test_summary_prompt_is_non_empty(self) -> None:
        from secondsight.analysis.prompts.summary import build_summary_prompt

        result = build_summary_prompt("sess-1", "proj-1", [_segment_analysis()])
        assert len(result) > 100

    def test_session_context_passes_through(self) -> None:
        from secondsight.analysis.prompts.summary import build_summary_prompt

        result = build_summary_prompt("sess-A", "proj-X", [_segment_analysis()])
        assert "sess-A" in result
        assert "proj-X" in result
        assert "segment_count: 1" in result

    def test_empty_segments_still_renders(self) -> None:
        from secondsight.analysis.prompts.summary import build_summary_prompt

        result = build_summary_prompt("sess-1", "proj-1", [])
        assert "segment_count: 0" in result

    def test_summary_prompt_determinism(self) -> None:
        from secondsight.analysis.prompts.summary import build_summary_prompt

        segs = [_segment_analysis()]
        assert build_summary_prompt("sess-1", "proj-1", segs) == build_summary_prompt(
            "sess-1", "proj-1", segs
        )

    def test_segment_payload_passes_through(self) -> None:
        from secondsight.analysis.prompts.summary import build_summary_prompt

        result = build_summary_prompt("sess-1", "proj-1", [_segment_analysis(flagged=2)])
        assert "agent read one extra unrelated file" in result
        assert "unnecessary_read" in result


# =====================================================================
# AGGREGATE PROMPT COMPAT
# =====================================================================


class TestAggregatePromptCompat:
    """Verify refactored build_aggregate_prompt is backward-compatible."""

    def test_aggregate_prompt_is_non_empty(self) -> None:
        from secondsight.analysis.prompts.aggregate import (
            build_aggregate_prompt,
        )

        result = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, [])
        assert len(result) > 100

    def test_flag_type_value_appears(self) -> None:
        from secondsight.analysis.prompts.aggregate import build_aggregate_prompt

        result = build_aggregate_prompt(BehaviorFlagType.MISSED_SHORTCUT, [])
        assert "missed_shortcut" in result

    def test_rejects_string_flag_type(self) -> None:
        from secondsight.analysis.prompts.aggregate import build_aggregate_prompt

        with pytest.raises(TypeError):
            build_aggregate_prompt("unnecessary_read", [])  # type: ignore[arg-type]

    def test_aggregate_prompt_determinism(self) -> None:
        from secondsight.analysis.prompts.aggregate import (
            FlagSummary,
            build_aggregate_prompt,
        )

        flags = [
            FlagSummary(
                session_id="sess-1",
                segment_summary="agent read README before editing",
                reason="README unrelated to the bug fix",
            )
        ]
        a = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, flags)
        b = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, flags)
        assert a == b

    def test_aggregate_prompt_flag_summary_payload_passes_through(self) -> None:
        from secondsight.analysis.prompts.aggregate import (
            FlagSummary,
            build_aggregate_prompt,
        )

        flags = [
            FlagSummary(
                session_id="sess-1",
                segment_summary="agent read README before editing target file",
                reason="README unrelated to the bug fix",
            )
        ]
        result = build_aggregate_prompt(BehaviorFlagType.UNNECESSARY_READ, flags)
        assert "sess-1" in result
        assert "agent read README before editing target file" in result
        assert "README unrelated to the bug fix" in result
