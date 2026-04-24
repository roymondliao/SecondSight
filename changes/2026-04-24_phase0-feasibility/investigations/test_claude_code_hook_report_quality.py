"""
Death tests and unit tests for the Claude Code hook investigation report.

Death tests (DC-1 variants): Verify the report doesn't claim feasibility
based on superficial evidence — shallow hook count, missing payload
field verification, or missed hook types.

Unit tests: Verify coverage math, schema completeness, and cross-validation
evidence requirements.

Run: pytest changes/2026-04-24_phase0-feasibility/investigations/test_claude_code_hook_report_quality.py -v
"""

import yaml
from pathlib import Path

YAML_REPORT_PATH = Path(__file__).parent / "claude-code-hooks.yaml"


def load_report():
    """Load the YAML report. Fail clearly if it doesn't exist."""
    assert YAML_REPORT_PATH.exists(), (
        f"Report file not found: {YAML_REPORT_PATH}\n"
        "The investigation YAML must be written before tests can pass."
    )
    with open(YAML_REPORT_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# DEATH TESTS — target silent failure paths first
# ---------------------------------------------------------------------------


class TestDeathCase1_ShallowFeasibilityByHookCount:
    """
    DC-1: Investigation reports 'feasible' based on hook count alone,
    but event payloads lack tool call arguments/results.

    Silent failure mode: coverage_rate is computed as
    (hook_types_found / needed_event_types) without verifying
    that each hook's payload actually contains required fields.
    This would cause Phase 2 to be designed around assumptions
    that don't hold at payload level.
    """

    def test_hook_types_have_payload_fields_documented(self):
        """Every hook_type entry MUST document payload_fields — not just name and trigger."""
        report = load_report()
        hook_types = report.get("hook_types", [])
        assert len(hook_types) > 0, "hook_types list must not be empty"

        for hook in hook_types:
            name = hook.get("name", "<unnamed>")
            payload_fields = hook.get("payload_fields", [])
            assert isinstance(payload_fields, list) and len(payload_fields) > 0, (
                f"Hook '{name}' has no payload_fields documented. "
                "Claiming feasibility without field-level evidence is DC-1 silent failure."
            )

    def test_tool_call_args_field_documented_in_pre_or_post_tool_use(self):
        """
        The payload MUST document a field containing tool call arguments (input/parameters).
        This is the minimum for Phase 2 action classification.
        Without it, SecondSight can only classify tool NAME but not what it did.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        tool_hooks = [
            h for h in hook_types
            if any(kw in h.get("name", "").lower() for kw in ["pretool", "posttool", "tool"])
        ]
        assert len(tool_hooks) > 0, (
            "No tool-related hook types found. PreToolUse/PostToolUse are known to exist."
        )

        found_input_field = False
        for hook in tool_hooks:
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                if any(kw in field_name for kw in ["input", "argument", "param", "tool_input"]):
                    found_input_field = True
                    break

        assert found_input_field, (
            "No tool input/argument field found in any PreToolUse/PostToolUse hook. "
            "DC-1: report cannot claim feasibility for tool call classification "
            "without documenting the field that carries tool arguments."
        )

    def test_tool_call_result_field_documented_in_post_tool_use(self):
        """
        PostToolUse MUST document a field containing tool call result/response.
        Without this, SecondSight can observe tool invocations but not outcomes —
        which breaks failure attribution in Phase 2.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        post_tool_hooks = [
            h for h in hook_types
            if "posttool" in h.get("name", "").lower()
        ]
        assert len(post_tool_hooks) > 0, (
            "PostToolUse hook not documented. It is confirmed to exist in official settings.json."
        )

        found_result_field = False
        for hook in post_tool_hooks:
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                if any(kw in field_name for kw in ["response", "result", "output", "tool_response"]):
                    found_result_field = True
                    break

        assert found_result_field, (
            "PostToolUse hook documents no result/response/output field. "
            "DC-1: Without tool results, failure attribution in Phase 2 is impossible."
        )

    def test_sufficient_field_event_types_less_than_available(self):
        """
        sufficient_field_event_types MUST be <= available_event_types.
        If they are equal, it may mean the investigator rubber-stamped all hooks
        as 'sufficient' without actually verifying field completeness.
        This test flags when all hooks are marked sufficient — requires justification.
        """
        report = load_report()
        coverage = report.get("coverage", {})
        available = coverage.get("available_event_types", 0)
        sufficient = coverage.get("sufficient_field_event_types", 0)

        assert sufficient <= available, (
            f"sufficient_field_event_types ({sufficient}) > available_event_types ({available}). "
            "This is mathematically impossible and indicates a report error."
        )

        # If all hooks are marked sufficient, there must be at least one limitation noted
        if sufficient == available and available > 0:
            all_limitations = []
            for hook in report.get("hook_types", []):
                lim = hook.get("limitations", "")
                if lim and lim.strip() and lim.strip().lower() not in ("none", "n/a", "-"):
                    all_limitations.append(lim)
            assert len(all_limitations) > 0, (
                f"All {available} hooks marked 'sufficient' but no limitations documented anywhere. "
                "DC-1: At minimum, payload stability risk must be noted as a limitation."
            )


class TestDeathCase2_JSONLTranscriptStabilityAssumption:
    """
    DC-2: Investigation treats JSONL transcript file watching as equivalent
    to a supported hook API — but the transcript is an internal format.

    Silent failure mode: SecondSight is built on JSONL transcript watching
    because it provides richer data (full message content, token counts).
    But if Claude Code changes the internal format, the entire observation
    layer breaks silently with no error — just missing data.
    """

    def test_risks_list_contains_jsonl_stability_risk(self):
        """
        The risks section MUST document that JSONL transcript format is internal
        and subject to change, distinct from the hook API.
        """
        report = load_report()
        risks = report.get("risks", [])
        assert len(risks) > 0, "risks list must not be empty"

        risk_text_combined = " ".join(str(r).lower() for r in risks)
        assert any(kw in risk_text_combined for kw in ["jsonl", "transcript", "internal", "unstable", "undocumented"]), (
            "DC-2: risks section does not mention JSONL transcript stability. "
            "JSONL transcript watching is unverified as a stable API — this risk must be documented."
        )

    def test_hooks_and_transcript_distinguished(self):
        """
        The hook_types MUST be distinct from JSONL transcript watching.
        If the report conflates 'hook events' with 'transcript file watching',
        coverage_rate is inflated with data that comes from an unofficial source.
        """
        report = load_report()
        # At minimum, one hook type must be documented as official hook (stdin-based)
        hook_types = report.get("hook_types", [])
        official_hook_names = {"pretooluse", "posttooluse", "stop", "subagentstart", "subagentsstop",
                               "subagent_start", "subagent_stop", "userpromptsubmit",
                               "permissionrequest", "posttoolusefeailure", "notification"}
        found_official = any(
            h.get("name", "").lower().replace("-", "").replace("_", "") in
            {n.replace("_", "") for n in official_hook_names}
            for h in hook_types
        )
        assert found_official, (
            "No official hook types (PreToolUse, PostToolUse, Stop, etc.) found in hook_types. "
            "DC-2: Report may be conflating transcript watching with hook events."
        )


class TestDeathCase3_MissingHookTypes:
    """
    DC-3: Investigation misses hook types that exist in official usage
    but aren't in the reference projects.

    Evidence from user's actual settings.json shows:
    - UserPromptSubmit (not in any reference project)
    - PostToolUseFailure (not in any reference project)
    - PermissionRequest (not in any reference project)
    - Notification (not documented in reference projects)

    Silent failure: SecondSight is designed around only PreToolUse/PostToolUse/Stop
    and misses UserPromptSubmit (which provides user prompt content)
    and PostToolUseFailure (which provides error signals).
    """

    def test_user_prompt_submit_hook_documented(self):
        """
        UserPromptSubmit MUST be documented. It fires when a user submits a prompt —
        this is the primary source of 'user prompt content' that Phase 2 needs.
        Evidence: confirmed in user's ~/.claude/settings.json.
        """
        report = load_report()
        hook_names = [h.get("name", "").lower().replace("_", "").replace("-", "") for h in report.get("hook_types", [])]
        assert "userpromptsubmit" in hook_names, (
            "DC-3: UserPromptSubmit hook not documented. "
            "This hook exists in the user's settings.json and is the primary source "
            "of user prompt content — required for Phase 2 prompt analysis."
        )

    def test_post_tool_use_failure_hook_documented(self):
        """
        PostToolUseFailure MUST be documented. It fires when a tool call fails —
        this is the primary signal for failure attribution in Phase 2.
        Evidence: confirmed in user's ~/.claude/settings.json.
        """
        report = load_report()
        hook_names_raw = [h.get("name", "").lower().replace("_", "").replace("-", "") for h in report.get("hook_types", [])]
        # Accept either PostToolUseFailure or an aliased name
        found = any("tooluseifailure" in n or "toolusefailure" in n or "posttoolusef" in n for n in hook_names_raw)
        assert found, (
            "DC-3: PostToolUseFailure hook not documented. "
            "This hook exists in the user's settings.json and is required for "
            "failure attribution. Missing it means Phase 2 cannot detect tool errors via hooks."
        )

    def test_permission_request_hook_documented(self):
        """
        PermissionRequest MUST be documented. It fires when Claude requests permission —
        relevant for governance analysis in Phase 3B.
        Evidence: confirmed in user's ~/.claude/settings.json.
        """
        report = load_report()
        hook_names_raw = [h.get("name", "").lower().replace("_", "").replace("-", "") for h in report.get("hook_types", [])]
        found = any("permission" in n for n in hook_names_raw)
        assert found, (
            "DC-3: PermissionRequest hook not documented. "
            "Evidence from settings.json confirms it exists. "
            "Required for Phase 3B governance analysis."
        )

    def test_hook_count_reflects_complete_set(self):
        """
        If investigation found fewer than 5 hook types, it likely missed some.
        Known minimum from evidence: PreToolUse, PostToolUse, Stop, SubagentStart,
        SubagentStop, UserPromptSubmit, PostToolUseFailure, PermissionRequest = 8.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])
        assert len(hook_types) >= 6, (
            f"Only {len(hook_types)} hook types documented. "
            "DC-3: Evidence shows at least 6-8 hook types exist. "
            "Investigation may have only consulted reference projects (which use 4-5) "
            "and missed hooks documented in official sources."
        )


# ---------------------------------------------------------------------------
# UNIT TESTS — verify report correctness (not just existence)
# ---------------------------------------------------------------------------


class TestReportStructure:
    """Verify the YAML report has required top-level fields."""

    def test_required_top_level_fields(self):
        report = load_report()
        required = ["agent", "investigation_date", "source", "verdict", "hook_types",
                    "coverage", "risks", "missing"]
        for field in required:
            assert field in report, f"Required top-level field '{field}' missing from report"

    def test_verdict_is_valid_value(self):
        report = load_report()
        valid_verdicts = {"feasible", "partially_feasible", "infeasible", "inconclusive"}
        assert report.get("verdict") in valid_verdicts, (
            f"verdict '{report.get('verdict')}' is not one of {valid_verdicts}"
        )

    def test_coverage_has_required_fields(self):
        report = load_report()
        coverage = report.get("coverage", {})
        required_coverage_fields = [
            "needed_event_types", "available_event_types",
            "sufficient_field_event_types", "coverage_rate"
        ]
        for field in required_coverage_fields:
            assert field in coverage, f"coverage.{field} missing"

    def test_coverage_rate_is_numeric(self):
        report = load_report()
        rate = report.get("coverage", {}).get("coverage_rate")
        assert isinstance(rate, (int, float)), (
            f"coverage_rate must be numeric, got {type(rate)}"
        )
        assert 0 <= rate <= 100, (
            f"coverage_rate {rate} out of range [0, 100]"
        )

    def test_agent_is_claude_code(self):
        report = load_report()
        assert report.get("agent") == "claude_code", (
            f"agent must be 'claude_code', got '{report.get('agent')}'"
        )


class TestCoverageCalculation:
    """Verify coverage math is consistent."""

    def test_sufficient_does_not_exceed_available(self):
        report = load_report()
        coverage = report.get("coverage", {})
        available = coverage.get("available_event_types", 0)
        sufficient = coverage.get("sufficient_field_event_types", 0)
        assert sufficient <= available, (
            f"sufficient ({sufficient}) > available ({available}) — math error in coverage"
        )

    def test_available_does_not_exceed_hook_type_count(self):
        """available_event_types should not exceed the number of documented hook types."""
        report = load_report()
        coverage = report.get("coverage", {})
        available = coverage.get("available_event_types", 0)
        hook_count = len(report.get("hook_types", []))
        assert available <= hook_count, (
            f"available_event_types ({available}) > len(hook_types) ({hook_count}). "
            "Coverage claims more available types than are documented."
        )

    def test_needed_event_types_covers_secondsight_requirements(self):
        """
        needed_event_types must be at least 5 to cover SecondSight's minimum:
        tool_call_type, tool_arguments, tool_results, timestamps, session_lifecycle.
        If needed < 5, the investigation understated requirements.
        """
        report = load_report()
        needed = report.get("coverage", {}).get("needed_event_types", 0)
        assert needed >= 5, (
            f"needed_event_types = {needed}, but SecondSight needs at least 5 event types. "
            "Investigation likely understated requirements."
        )


class TestSecondSightNeeds:
    """Verify that each of SecondSight's required data points is addressed."""

    REQUIRED_ANALYSES = [
        ("tool_call_type", "tool call type classification"),
        ("tool_call_args", "tool call arguments"),
        ("timestamps", "timing / start / end timestamps"),
        ("session", "session lifecycle"),
    ]

    def test_missing_section_documents_gaps(self):
        """
        The 'missing' section must exist and document what SecondSight cannot get.
        An empty missing list is a red flag — no investigation is perfect.
        """
        report = load_report()
        missing = report.get("missing", [])
        # missing CAN be empty if all needs are met — but then coverage must be high
        coverage_rate = report.get("coverage", {}).get("coverage_rate", 0)
        if len(missing) == 0:
            assert coverage_rate >= 90, (
                "missing list is empty but coverage_rate < 90%. "
                "Either document what's missing or achieve high coverage."
            )

    def test_token_usage_gap_acknowledged(self):
        """
        Token usage per call is a required field for SecondSight.
        Either it's available (documented in payload_fields) or it's in 'missing'.
        This must be explicitly addressed — not silently omitted.
        """
        report = load_report()

        # Check if any hook documents token usage
        found_token_field = False
        for hook in report.get("hook_types", []):
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                if any(kw in field_name for kw in ["token", "usage", "cost"]):
                    found_token_field = True
                    break

        if not found_token_field:
            # If hooks don't provide token data, it must be in missing or addressed via transcript
            missing_text = " ".join(str(m).lower() for m in report.get("missing", []))
            risks_text = " ".join(str(r).lower() for r in report.get("risks", []))
            all_text = missing_text + " " + risks_text
            assert "token" in all_text, (
                "Token usage is required by SecondSight but neither documented in any "
                "hook payload_fields nor listed in 'missing'. This gap is silently omitted."
            )

    def test_user_prompt_content_source_identified(self):
        """
        User prompt content is required for Phase 2.
        Either UserPromptSubmit hook provides it, or JSONL transcript watching does —
        but the source must be identified, and if it's transcript-only, that's a risk.
        """
        report = load_report()
        # Check hooks for user prompt field
        found_prompt = False
        for hook in report.get("hook_types", []):
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                use = field.get("analysis_use", "").lower()
                if any(kw in field_name + " " + use for kw in ["prompt", "user_message", "user_content"]):
                    found_prompt = True
                    break

        # Also acceptable if transcript section or risks covers this
        all_text = (
            " ".join(str(r).lower() for r in report.get("risks", []))
            + " " + str(report).lower()
        )
        if not found_prompt:
            assert "prompt" in all_text or "transcript" in all_text, (
                "User prompt content is required but no source is identified. "
                "Either document it in hook payload_fields or note the transcript dependency."
            )
