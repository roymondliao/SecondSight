"""Death tests for secondsight.prompts._loader (Task 3).

Death-first ordering: silent-failure paths first, happy paths after.

Critical death cases:
- DC9: template renders with a missing context variable → UndefinedError
  (NOT silent empty render). StrictUndefined must raise, not coerce to "".
- TemplateNotFound: non-existent template name raises TemplateNotFound,
  not returns empty string.
- Clean render: rendered output contains no literal {{ or }} (template
  expansion completed fully).
- autoescape=False: analysis prompts are NOT HTML; escaping corrupts
  JSON/code blocks embedded in prompts.
"""

from __future__ import annotations

import pytest
import jinja2

from secondsight.prompts._loader import render


# =====================================================================
# DEATH TESTS — DC9 and TemplateNotFound
# =====================================================================


class TestDeathPaths:
    def test_dt_dc9_missing_context_variable_raises_undefined_error(self) -> None:
        """DC9 — StrictUndefined must raise jinja2.UndefinedError when a
        context variable is missing. Silent empty render is forbidden.

        If StrictUndefined is NOT configured (e.g., default Undefined),
        the template renders with the missing variable as empty string —
        the CLI dispatcher gets a prompt missing critical context and the
        coding agent produces no behavior_flags. This is the silent data
        loss path.
        """
        # behavior.jinja2 references session_id (via segment data) among
        # other variables. We deliberately pass an empty context.
        with pytest.raises(jinja2.UndefinedError) as exc_info:
            render("analysis/behavior", context={})
        # The error message should mention the missing variable name
        # (jinja2 UndefinedError always includes the variable name).
        assert exc_info.value is not None

    def test_dt_template_not_found_raises_properly(self) -> None:
        """Non-existent template raises TemplateNotFound, not returns
        empty string or None. If a template path typo silently returns
        empty, the CLI dispatcher gets an empty prompt.
        """
        with pytest.raises(jinja2.TemplateNotFound):
            render("nonexistent/template", context={})

    def test_dt_render_result_contains_no_unrendered_jinja_markers(
        self,
        behavior_context: dict,
    ) -> None:
        """A fully-rendered template must contain no literal {{ or }}.
        If {{ or }} remain, the template expansion was incomplete —
        the coding agent receives un-expanded variable references
        and cannot interpret them.
        """
        result = render("analysis/behavior", context=behavior_context)
        assert "{{" not in result, (
            "Rendered output contains literal {{ — template not fully expanded"
        )
        assert "}}" not in result, (
            "Rendered output contains literal }} — template not fully expanded"
        )

    def test_dt_autoescape_is_disabled(self, behavior_context: dict) -> None:
        """autoescape=False — analysis prompts contain JSON content and
        code blocks. If autoescape=True, characters like < > & " ' in
        JSON schema blocks get HTML-entity-encoded, corrupting the
        schema the coding agent reads. This verifies the env is configured
        with autoescape=False by passing a context value with HTML chars
        and asserting it is NOT escaped.
        """
        # Inject a value with HTML-special characters into the context
        ctx = dict(behavior_context)
        # Override segment_json with a value containing HTML chars
        ctx["segment_json"] = '{"key": "<value> & \'test\'"}'
        result = render("analysis/behavior", context=ctx)
        # If autoescape were True, < would become &lt; etc.
        assert "&lt;" not in result, "autoescape is ON — HTML encoding corrupts JSON content"
        assert "&amp;" not in result, "autoescape is ON — HTML encoding corrupts JSON content"

    def test_dt_schema_version_literal_in_behavior_render(
        self,
        behavior_context: dict,
    ) -> None:
        """The AnalysisOutput JSON schema is embedded in behavior.jinja2
        via the analysis_output_schema context variable. The schema
        contains 'schema_version' as a field name. Verifying its presence
        ensures the schema was actually rendered (not an empty string or
        placeholder).

        If schema is missing, coding agents receive no output format
        spec and produce non-conformant JSON.
        """
        result = render("analysis/behavior", context=behavior_context)
        assert "schema_version" in result, (
            "AnalysisOutput schema not embedded in behavior prompt — "
            "coding agent has no output format specification"
        )


# =====================================================================
# HAPPY PATH TESTS
# =====================================================================


class TestHappyPaths:
    def test_render_behavior_returns_non_empty_string(
        self,
        behavior_context: dict,
    ) -> None:
        result = render("analysis/behavior", context=behavior_context)
        assert isinstance(result, str)
        assert len(result) > 100  # meaningful content, not trivial

    def test_render_summary_returns_non_empty_string(
        self,
        summary_context: dict,
    ) -> None:
        result = render("analysis/summary", context=summary_context)
        assert isinstance(result, str)
        assert len(result) > 100

    def test_render_aggregate_returns_non_empty_string(
        self,
        aggregate_context: dict,
    ) -> None:
        result = render("analysis/aggregate", context=aggregate_context)
        assert isinstance(result, str)
        assert len(result) > 100

    def test_render_is_deterministic(self, behavior_context: dict) -> None:
        """Same context always produces same string. Required for
        prompt-caching strategies in the orchestrator.
        """
        a = render("analysis/behavior", context=behavior_context)
        b = render("analysis/behavior", context=behavior_context)
        assert a == b


# =====================================================================
# FIXTURES
# =====================================================================


@pytest.fixture
def behavior_context() -> dict:
    """Minimal valid context for analysis/behavior.jinja2."""
    import json
    from secondsight.analysis.output import AnalysisOutput
    from secondsight.analysis.schemas import (
        FLAG_DEFINITIONS,
        BehaviorFlagType,
    )

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
    """Minimal valid context for analysis/summary.jinja2."""
    import json

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
    """Minimal valid context for analysis/aggregate.jinja2."""
    import json

    return {
        "flag_type_value": "unnecessary_read",
        "flag_type_description": "Read a file unrelated to the current task intent",
        "flags_json": json.dumps([], ensure_ascii=False, sort_keys=True, indent=2),
    }
