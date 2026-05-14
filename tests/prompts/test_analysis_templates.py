"""Structural tests for analysis/*.jinja2 template content (Task 3).

These tests verify the rendered output from all three templates contains
the expected structural sections and content — distinct from test_loader.py
(which tests the loader mechanics) and test_render_compat.py (which tests
backward compat vs. old string constants).
"""

from __future__ import annotations

import json

import pytest

from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    BehaviorFlagType,
)
from secondsight.analysis.output import AnalysisOutput
from secondsight.prompts._loader import render


# =====================================================================
# BEHAVIOR TEMPLATE
# =====================================================================


class TestBehaviorTemplate:
    def test_all_six_flag_types_appear_in_rendered_prompt(self, behavior_context: dict) -> None:
        """Every BehaviorFlagType enum member must appear in the rendered
        prompt. A missing flag type silently trains the LLM to never
        produce it (same death case as DT-B1 in old test_behavior.py).
        """
        result = render("analysis/behavior", context=behavior_context)
        for flag_type in BehaviorFlagType:
            assert flag_type.value in result, (
                f"flag_type {flag_type.value!r} missing from rendered behavior prompt"
            )

    def test_all_required_sections_present(self, behavior_context: dict) -> None:
        result = render("analysis/behavior", context=behavior_context)
        for section in (
            "[System]",
            "[Schema]",
            "[Flag Type Definitions]",
            "[Task]",
            "[Segment Data]",
            "[Output Format]",
        ):
            assert section in result, f"Section {section!r} missing from behavior template"

    def test_confidence_field_appears_in_output_format(self, behavior_context: dict) -> None:
        """The prompt must instruct the LLM to emit the `confidence`
        field with the three permitted values. Missing this causes every
        model response to fail Pydantic validation.
        """
        result = render("analysis/behavior", context=behavior_context)
        assert "confidence" in result
        assert "high" in result
        assert "medium" in result
        assert "low" in result

    def test_analysis_output_schema_embedded(self, behavior_context: dict) -> None:
        """AnalysisOutput.model_json_schema() must be embedded so the
        coding agent (CLI mode) knows the exact output shape.
        """
        result = render("analysis/behavior", context=behavior_context)
        assert "schema_version" in result

    def test_segment_json_passes_through(self, behavior_context: dict) -> None:
        result = render("analysis/behavior", context=behavior_context)
        # The segment payload JSON was in behavior_context["segment_json"]
        assert "segment_index" in result
        assert "supplementary_metrics" in result


# =====================================================================
# SUMMARY TEMPLATE
# =====================================================================


class TestSummaryTemplate:
    def test_session_id_appears_in_render(self, summary_context: dict) -> None:
        result = render("analysis/summary", context=summary_context)
        assert "sess-test" in result

    def test_project_id_appears_in_render(self, summary_context: dict) -> None:
        result = render("analysis/summary", context=summary_context)
        assert "proj-test" in result

    def test_segment_count_appears_in_render(self, summary_context: dict) -> None:
        result = render("analysis/summary", context=summary_context)
        assert "1" in result  # segment_count

    def test_all_required_sections_present(self, summary_context: dict) -> None:
        result = render("analysis/summary", context=summary_context)
        for section in (
            "[System]",
            "[Schema]",
            "[Session Context]",
            "[Task]",
            "[Segments]",
            "[Output Format]",
        ):
            assert section in result, f"Section {section!r} missing from summary template"

    def test_segments_json_passes_through(self, summary_context: dict) -> None:
        result = render("analysis/summary", context=summary_context)
        assert "test summary" in result


# =====================================================================
# AGGREGATE TEMPLATE
# =====================================================================


class TestAggregateTemplate:
    def test_flag_type_value_appears(self, aggregate_context: dict) -> None:
        result = render("analysis/aggregate", context=aggregate_context)
        assert "unnecessary_read" in result

    def test_flag_type_description_appears(self, aggregate_context: dict) -> None:
        result = render("analysis/aggregate", context=aggregate_context)
        assert "unrelated to the current task intent" in result

    def test_all_required_sections_present(self, aggregate_context: dict) -> None:
        result = render("analysis/aggregate", context=aggregate_context)
        for section in ("[System]", "[Task]", "[Behavior Flags]", "[Output Format]"):
            assert section in result, f"Section {section!r} missing from aggregate template"

    def test_flags_json_passes_through(self, aggregate_context: dict) -> None:
        result = render("analysis/aggregate", context=aggregate_context)
        assert "[]" in result  # empty flags rendered as JSON array


# =====================================================================
# FIXTURES
# =====================================================================


@pytest.fixture
def behavior_context() -> dict:
    flag_defs_lines = []
    for flag_type in BehaviorFlagType:
        defn = FLAG_DEFINITIONS[flag_type]
        flag_defs_lines.append(f"- {flag_type.value}")
        flag_defs_lines.append(f"  description: {defn['description']}")
        flag_defs_lines.append(f"  criteria: {defn['criteria']}")
        flag_defs_lines.append(f"  example: {defn['example']}")
        flag_defs_lines.append("")
    flag_definitions_block = "\n".join(flag_defs_lines).rstrip()

    segment_payload = {
        "segment_index": 0,
        "user_prompt": None,
        "events": [],
        "supplementary_metrics": {
            "total_tokens": 100,
            "unique_files": 1,
            "duration": 1.0,
            "error_count": 0,
        },
    }

    return {
        "segment_json": json.dumps(segment_payload, ensure_ascii=False, sort_keys=True, indent=2),
        "flag_definitions_block": flag_definitions_block,
        "analysis_output_schema": json.dumps(AnalysisOutput.model_json_schema(), indent=2),
    }


@pytest.fixture
def summary_context() -> dict:
    return {
        "session_id": "sess-test",
        "project_id": "proj-test",
        "segment_count": 1,
        "segments_json": json.dumps(
            [
                {
                    "segment_summary": "test summary",
                    "flags": [],
                    "total_events": 3,
                    "flagged_events": 0,
                }
            ],
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
    }


@pytest.fixture
def aggregate_context() -> dict:
    return {
        "flag_type_value": "unnecessary_read",
        "flag_type_description": "Read a file unrelated to the current task intent",
        "flags_json": json.dumps([], ensure_ascii=False, sort_keys=True, indent=2),
    }
