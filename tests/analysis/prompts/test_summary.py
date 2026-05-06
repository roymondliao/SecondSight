"""Death + coverage tests for analysis.prompts.summary (GUR-101 P2-7).

Note: the [任務] block in summary.py is intentionally a TODO placeholder
pending the project lead's UX decision. These tests cover structural
death cases (output schema bounds, prompt section presence,
determinism) that hold regardless of the final task-block wording.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secondsight.analysis.prompts.summary import (
    SummaryOutput,
    build_summary_prompt,
)
from secondsight.analysis.schemas import (
    BehaviorFlagDraft,
    BehaviorFlagType,
    SegmentAnalysis,
)


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
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_s1_rejects_more_than_five_key_findings(self) -> None:
        """DT-S1 — `key_findings` is bounded ≤ 5 (dashboard list-view
        constraint). A model response with more findings fails
        validation rather than silently truncate downstream.
        """
        with pytest.raises(ValidationError):
            SummaryOutput(
                headline="x",
                key_findings=["a", "b", "c", "d", "e", "f"],
                body="y",
            )

    def test_dt_s2_rejects_empty_headline(self) -> None:
        """DT-S2 — an empty headline would leave the dashboard card
        blank. Validation catches it.
        """
        with pytest.raises(ValidationError):
            SummaryOutput(headline="", key_findings=[], body="y")

    def test_dt_s3_rejects_headline_exceeding_max_length(self) -> None:
        with pytest.raises(ValidationError):
            SummaryOutput(headline="x" * 201, key_findings=[], body="y")


# =====================================================================
# COVERAGE TESTS
# =====================================================================


class TestBuildSummaryPrompt:
    def test_pure_function_determinism(self) -> None:
        segs = [_segment_analysis()]
        a = build_summary_prompt("sess-1", "proj-1", segs)
        b = build_summary_prompt("sess-1", "proj-1", segs)
        assert a == b

    def test_session_context_appears_in_prompt(self) -> None:
        prompt = build_summary_prompt("sess-A", "proj-X", [_segment_analysis()])
        assert "session_id: sess-A" in prompt
        assert "project_id: proj-X" in prompt
        assert "segment_count: 1" in prompt

    def test_empty_segments_still_renders(self) -> None:
        """Coverage — a session with zero analyzed segments is rare
        but possible (bound at ingest time). The prompt renders
        cleanly with segment_count=0; orchestrator policy decides
        whether to call the LLM in that case.
        """
        prompt = build_summary_prompt("sess-1", "proj-1", [])
        assert "segment_count: 0" in prompt

    def test_includes_required_section_headers(self) -> None:
        prompt = build_summary_prompt("sess-1", "proj-1", [_segment_analysis()])
        for header in (
            "[System]",
            "[Schema 說明]",
            "[Session Context]",
            "[任務]",
            "[Segments]",
            "[Output Format]",
        ):
            assert header in prompt

    def test_segment_payload_passes_through(self) -> None:
        seg = _segment_analysis(flagged=2)
        prompt = build_summary_prompt("sess-1", "proj-1", [seg])
        assert "agent read one extra unrelated file" in prompt
        assert "unnecessary_read" in prompt


class TestSummaryOutput:
    def test_accepts_well_formed_response(self) -> None:
        SummaryOutput(
            headline="Mostly efficient session, 2 flags",
            key_findings=[
                "Read README before editing the target file",
                "Re-grepped after a Read returned an answer",
            ],
            body="Across 5 segments the agent ...",
        )

    def test_accepts_empty_key_findings(self) -> None:
        SummaryOutput(headline="Clean session", key_findings=[], body="")
