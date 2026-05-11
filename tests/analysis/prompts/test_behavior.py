"""Death + coverage tests for analysis.prompts.behavior (GUR-101 P2-5).

Death-first ordering: silent-failure paths first, happy paths after.
The most important death test is DT-B1 — every BehaviorFlagType enum
member must appear in the rendered prompt, because a missing flag
type would silently train the LLM never to produce it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secondsight.analysis.prompts.behavior import (
    build_segment_prompt,
    render_flag_definitions,
)
from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    BehaviorFlagDraft,
    BehaviorFlagType,
    SegmentAnalysis,
    SegmentData,
    SegmentMetrics,
)


def _segment(events: list = (), user_prompt=None) -> SegmentData:
    return SegmentData(
        segment_index=1,
        user_prompt=user_prompt,
        events=list(events),
        session_id="sess-1",
        project_id="proj-1",
    )


def _metrics() -> SegmentMetrics:
    return SegmentMetrics(total_tokens=1234, unique_files=2, duration=3.5, error_count=0)


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_b1_every_flag_type_appears_in_rendered_prompt(self) -> None:
        """DT-B1 — silent-omission death: a missing flag type would
        train the LLM to never produce it. The renderer iterates the
        enum so any missing FLAG_DEFINITIONS key surfaces as KeyError;
        this test asserts the resulting string actually contains every
        enum value (not just the dict keys).
        """
        prompt = build_segment_prompt(_segment(), _metrics())
        for flag_type in BehaviorFlagType:
            assert flag_type.value in prompt, (
                f"flag_type {flag_type.value!r} missing from rendered "
                f"prompt — silent omission would suppress this category"
            )

    def test_dt_b2_render_flag_definitions_raises_on_missing_dict_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DT-B2 — if a future enum member is added without a
        FLAG_DEFINITIONS entry, the renderer raises KeyError at build
        time (NOT silently omits the unknown type from the prompt).
        """
        # Simulate the gap by removing one entry.
        clipped = dict(FLAG_DEFINITIONS)
        del clipped[BehaviorFlagType.UNNECESSARY_READ]
        monkeypatch.setattr(
            "secondsight.analysis.prompts.behavior.FLAG_DEFINITIONS",
            clipped,
        )
        with pytest.raises(KeyError):
            render_flag_definitions()

    def test_dt_b3_output_format_names_confidence_field(self) -> None:
        """DT-B3 — the prompt must instruct the LLM to emit the
        `confidence` field with the exact three permitted values.
        Missing this would make the orchestrator Pydantic validation
        reject every model response.
        """
        prompt = build_segment_prompt(_segment(), _metrics())
        assert '"confidence"' in prompt
        assert "high" in prompt
        assert "medium" in prompt
        assert "low" in prompt

    def test_dt_b4_pre_prompt_segment_renders_user_prompt_null(self) -> None:
        """DT-B4 — a segment_index=0 pre-prompt segment carries
        user_prompt=None per SD §5.3.1. The rendered prompt must
        surface that as null (not as the string "None"), otherwise
        the LLM cannot tell the difference between an absent prompt
        and a prompt whose text happens to be the literal "None".
        """
        prompt = build_segment_prompt(_segment(user_prompt=None), _metrics())
        # JSON null appears as `null` in the dumped Segment Data block.
        assert '"user_prompt": null' in prompt

    def test_dt_b5_segment_analysis_rejects_invalid_flag_in_output(
        self,
    ) -> None:
        """DT-B5 — SegmentAnalysis must reject an LLM response that
        invents a flag_type outside the enum. Catching this at
        validation time prevents bad rows from reaching the DB.
        """
        bad_payload = {
            "segment_summary": "ok",
            "flags": [
                {
                    "flag_type": "bogus_type",  # not in enum
                    "event_ids": ["e1"],
                    "reason": "fake",
                    "confidence": "high",
                }
            ],
            "total_events": 1,
            "flagged_events": 1,
        }
        with pytest.raises(ValidationError):
            SegmentAnalysis.model_validate(bad_payload)


# =====================================================================
# COVERAGE TESTS
# =====================================================================


class TestRenderFlagDefinitions:
    def test_renders_all_six_blocks(self) -> None:
        rendered = render_flag_definitions()
        for flag_type in BehaviorFlagType:
            assert flag_type.value in rendered
            defn = FLAG_DEFINITIONS[flag_type]
            assert defn["description"] in rendered
            assert defn["criteria"] in rendered
            assert defn["example"] in rendered

    def test_block_order_follows_enum_order(self) -> None:
        rendered = render_flag_definitions()
        positions = [rendered.index(ft.value) for ft in BehaviorFlagType]
        assert positions == sorted(positions), (
            "render order must follow BehaviorFlagType enum declaration "
            "order — important for golden-file determinism"
        )


class TestBuildSegmentPrompt:
    def test_pure_function_determinism(self) -> None:
        seg = _segment()
        m = _metrics()
        a = build_segment_prompt(seg, m)
        b = build_segment_prompt(seg, m)
        assert a == b, "build_segment_prompt must be deterministic"

    def test_includes_all_six_section_headers(self) -> None:
        prompt = build_segment_prompt(_segment(), _metrics())
        for header in (
            "[System]",
            "[Schema 說明]",
            "[Flag Type 定義]",
            "[任務]",
            "[Segment Data]",
            "[Output Format]",
        ):
            assert header in prompt

    def test_segment_metrics_dict_appears_in_payload(self) -> None:
        m = SegmentMetrics(total_tokens=42, unique_files=7, duration=1.5, error_count=3)
        prompt = build_segment_prompt(_segment(), m)
        assert '"total_tokens": 42' in prompt
        assert '"unique_files": 7' in prompt
        assert '"error_count": 3' in prompt

    def test_user_prompt_text_passes_through(self) -> None:
        prompt_text = "幫我修 utils.py 的 bug"
        prompt = build_segment_prompt(_segment(user_prompt={"text": prompt_text}), _metrics())
        assert prompt_text in prompt

    def test_segment_analysis_validates_well_formed_response(self) -> None:
        """Coverage — SD §5.5.2 happy path: a syntactically valid LLM
        response shaped like the SD example parses successfully.
        """
        payload = {
            "segment_summary": "Mostly efficient; one extra file read.",
            "flags": [
                {
                    "flag_type": "unnecessary_read",
                    "event_ids": ["e3"],
                    "reason": "config.yaml unrelated to bug fix",
                    "confidence": "high",
                }
            ],
            "total_events": 5,
            "flagged_events": 1,
        }
        parsed = SegmentAnalysis.model_validate(payload)
        assert len(parsed.flags) == 1
        assert isinstance(parsed.flags[0], BehaviorFlagDraft)
        assert parsed.flags[0].flag_type is BehaviorFlagType.UNNECESSARY_READ
