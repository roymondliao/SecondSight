"""Death + coverage tests for analysis.prompts.summary (GUR-101 P2-7).

Death tests cover output schema bounds (key_findings ≤ 5, headline
length); coverage tests pin the structural sections plus the UX
defaults encoded in `_TASK_BLOCK` (verdict+count headline format,
confidence-first ordering, event_id references, low-confidence
summary). Revising the UX defaults requires updating these coverage
assertions.
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
        # Section headers were updated to English in Task 3 jinja2 migration.
        prompt = build_summary_prompt("sess-1", "proj-1", [_segment_analysis()])
        for header in (
            "[System]",
            "[Schema]",
            "[Session Context]",
            "[Task]",
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


class TestTaskBlockUxDefaults:
    """Pin the UX decisions encoded in `_TASK_BLOCK`. Each assertion
    documents one ratified default; revising a default requires
    updating both `_TASK_BLOCK` and the assertion here so the rationale
    cannot drift silently.
    """

    def _prompt(self) -> str:
        return build_summary_prompt("sess-1", "proj-1", [_segment_analysis()])

    def test_headline_spec_uses_verdict_plus_count_format(self) -> None:
        """UX default 1: headline leads with verdict, then count. The
        prompt instructs `flags across segments` phrasing as the
        evidence anchor.
        """
        assert "flags across" in self._prompt()

    def test_neutral_observational_tone_is_named(self) -> None:
        """UX default 2: tone explicitly told to stay neutral and
        observational, not coaching or judgmental.
        Updated to English in Task 3 jinja2 migration.
        """
        prompt = self._prompt()
        assert "neutral" in prompt.lower()
        assert "observational" in prompt.lower()

    def test_key_findings_ordered_by_confidence_then_frequency(self) -> None:
        """UX default 3: confidence is the primary sort key, frequency
        secondary. High-confidence flags surface first.
        Updated to English in Task 3 jinja2 migration.
        """
        prompt = self._prompt()
        assert "confidence" in prompt
        assert "high" in prompt

    def test_body_references_event_ids_directly(self) -> None:
        """UX default 4: body must cite event_ids verbatim so the
        dashboard can hyperlink them to trace detail (GUR-106).
        Updated to English in Task 3 jinja2 migration.
        """
        assert "event_ids" in self._prompt()

    def test_low_confidence_flags_summarized_not_enumerated(self) -> None:
        """UX default 5: low-confidence flags get a single-line count
        summary; never per-flag enumeration. Avoids drowning the
        report in noise.
        Updated to English in Task 3 jinja2 migration.
        """
        prompt = self._prompt()
        assert "low-confidence" in prompt
        # The explicit omit-when-zero rule must be carried.
        assert "omit" in prompt
