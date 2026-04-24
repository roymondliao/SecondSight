"""
Death tests and unit tests for the OpenCode hook investigation report.

Death tests (DC-1 variants): Verify the report doesn't claim feasibility
based on superficial evidence — shallow hook count, missing payload
field verification, or stability assumptions that weren't verified.

Unit tests: Verify coverage math, schema completeness, and cross-validation
evidence requirements.

Run: pytest changes/2026-04-24_phase0-feasibility/investigations/test_opencode_hook_report_quality.py -v
"""

import yaml
from pathlib import Path

YAML_REPORT_PATH = Path(__file__).parent / "opencode-hooks.yaml"


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
    but event payloads lack tool call arguments/results needed for
    action classification.

    Silent failure mode: report says "OpenCode has tool.execute.before
    and tool.execute.after" (which is true) but does not verify that
    the payload actually contains args and output fields sufficient for
    Phase 2 action classification. Phase 2 team then builds assuming
    rich payloads, discovers the gap at integration time.
    """

    def test_tool_execute_before_documents_payload_fields(self):
        """
        tool.execute.before MUST have payload_fields documented.
        Known from source: input = {tool, sessionID, callID}, output.args = any.
        The `output.args` field is what carries tool arguments — it must be named.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        before_hooks = [
            h for h in hook_types
            if "before" in h.get("name", "").lower() or "tool.execute.before" in h.get("name", "").lower()
        ]
        assert len(before_hooks) > 0, (
            "tool.execute.before not found in hook_types. "
            "Confirmed in official docs and plugin type definitions."
        )
        for hook in before_hooks:
            payload_fields = hook.get("payload_fields", [])
            assert isinstance(payload_fields, list) and len(payload_fields) > 0, (
                f"Hook '{hook.get('name')}' documents no payload_fields. "
                "DC-1: Without field-level evidence, feasibility claim is hollow."
            )

    def test_tool_execute_after_documents_args_and_output_fields(self):
        """
        tool.execute.after MUST document both args (input) and output (result).
        Known from source: input = {tool, sessionID, callID, args: any},
        output = {title, output: string, metadata: any}.
        Both are required for Phase 2: args for classification, output for result analysis.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        after_hooks = [
            h for h in hook_types
            if "after" in h.get("name", "").lower() or "tool.execute.after" in h.get("name", "").lower()
        ]
        assert len(after_hooks) > 0, (
            "tool.execute.after not found in hook_types. "
            "Confirmed in official docs plugin type definitions."
        )

        found_args = False
        found_output = False
        for hook in after_hooks:
            for field in hook.get("payload_fields", []):
                name = field.get("name", "").lower()
                if any(kw in name for kw in ["arg", "input", "param"]):
                    found_args = True
                if any(kw in name for kw in ["output", "result", "response"]):
                    found_output = True

        assert found_args, (
            "DC-1: tool.execute.after does not document an args/input field. "
            "Without this, tool classification in Phase 2 is impossible from hook data."
        )
        assert found_output, (
            "DC-1: tool.execute.after does not document an output/result field. "
            "Without this, outcome tracking in Phase 2 is impossible from hook data."
        )

    def test_sufficient_field_event_types_less_than_or_equal_available(self):
        """
        sufficient_field_event_types MUST be <= available_event_types.
        If they are equal, some limitation must still be noted — no hook is perfect.
        """
        report = load_report()
        coverage = report.get("coverage", {})
        available = coverage.get("available_event_types", 0)
        sufficient = coverage.get("sufficient_field_event_types", 0)

        assert sufficient <= available, (
            f"sufficient_field_event_types ({sufficient}) > available_event_types ({available}). "
            "This is mathematically impossible and indicates a report error."
        )

        if sufficient == available and available > 0:
            all_limitations = []
            for hook in report.get("hook_types", []):
                lim = hook.get("limitations", "")
                if lim and lim.strip() and lim.strip().lower() not in ("none", "n/a", "-"):
                    all_limitations.append(lim)
            assert len(all_limitations) > 0, (
                f"All {available} hooks marked sufficient but no limitations documented. "
                "DC-1: At minimum, the lack of per-call timestamp from the hook (timestamps "
                "are reconstructed from DB writes) must be documented as a limitation."
            )


class TestDeathCase2_LazyagentVersionAssumption:
    """
    DC-2: Investigation assumes lazyagent's OpenCode integration matches
    the current OpenCode version without verifying.

    Silent failure mode: lazyagent reads from opencode.db directly (SQLite).
    The current OpenCode (sst/opencode) uses SQLite with a specific schema
    (session, message, part tables). lazyagent was built against this schema —
    but if the schema version diverges, lazyagent's approach stops working
    silently. The investigation must note whether the lazyagent schema
    was cross-validated against the current source.
    """

    def test_risks_list_mentions_schema_version_stability(self):
        """
        The risks section MUST document that OpenCode's SQLite schema can change
        and lazyagent cross-validation only proves the schema at one point in time.
        """
        report = load_report()
        risks = report.get("risks", [])
        assert len(risks) > 0, "risks list must not be empty"

        risk_text = " ".join(str(r).lower() for r in risks)
        assert any(kw in risk_text for kw in ["schema", "version", "unstable", "change", "migration", "evolv"]), (
            "DC-2: risks section does not mention schema stability. "
            "OpenCode's SQLite schema is internal and subject to change without notice — "
            "this risk must be documented."
        )

    def test_source_notes_cross_validation_performed(self):
        """
        The investigation's 'source' field must indicate cross-validation
        against reference source (lazyagent) was performed.
        """
        report = load_report()
        source = str(report.get("source", "")).lower()
        assert any(kw in source for kw in ["lazyagent", "reference", "cross", "opensoure", "source"]), (
            "DC-2: 'source' field does not indicate cross-validation with lazyagent. "
            "Investigation may have relied only on official docs without verifying "
            "against the working reference implementation."
        )


class TestDeathCase3_OpenSourceExtensionCostHidden:
    """
    DC-3: Investigation assumes OpenCode being open source means full access,
    but ignores that custom hook implementation requires ongoing maintenance cost.

    Silent failure mode: Investigation notes "feasible via plugins" but doesn't
    flag that the plugin system is JS/TS only, requires Bun runtime, and any
    custom hook code has a maintenance surface. If Phase 2 architects plan
    a Python-native solution, they'd be surprised by the Bun/JS constraint.
    """

    def test_risks_mentions_plugin_runtime_constraint(self):
        """
        Risks MUST mention that OpenCode plugins run in Bun (JS/TS) runtime,
        not Python. This is a cross-language integration cost.
        """
        report = load_report()
        risk_text = " ".join(str(r).lower() for r in report.get("risks", []))
        # Also check limitations of individual hooks
        hook_limitations = " ".join(
            str(h.get("limitations", "")).lower() for h in report.get("hook_types", [])
        )
        combined = risk_text + " " + hook_limitations

        assert any(kw in combined for kw in ["bun", "javascript", "typescript", "js", "plugin", "runtime", "language"]), (
            "DC-3: No mention of plugin runtime constraint (Bun/JS/TS). "
            "SecondSight is Python-based but OpenCode plugins require JS/TS. "
            "This cross-language cost must be documented."
        )

    def test_risks_or_missing_addresses_extension_maintenance(self):
        """
        The investigation must acknowledge that extending OpenCode's hook API
        requires maintaining custom plugin code — this is not zero-cost.
        """
        report = load_report()
        risk_text = " ".join(str(r).lower() for r in report.get("risks", []))
        missing_text = " ".join(str(m).lower() for m in report.get("missing", []))
        combined = risk_text + " " + missing_text

        assert any(kw in combined for kw in ["maintain", "custom", "extend", "plugin", "cost", "overhead", "open source"]), (
            "DC-3: Investigation does not address extension/maintenance cost. "
            "Open source != free maintenance. Custom plugin code has ongoing cost."
        )


# ---------------------------------------------------------------------------
# UNIT TESTS — verify report correctness (not just existence)
# ---------------------------------------------------------------------------


class TestReportStructure:
    """Verify the YAML report has required top-level fields."""

    def test_required_top_level_fields(self):
        report = load_report()
        required = ["agent", "investigation_date", "source", "verdict",
                    "hook_types", "coverage", "risks", "missing"]
        for field in required:
            assert field in report, f"Required top-level field '{field}' missing from report"

    def test_agent_is_opencode(self):
        report = load_report()
        assert report.get("agent") == "opencode", (
            f"agent must be 'opencode', got '{report.get('agent')}'"
        )

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

    def test_coverage_rate_is_numeric_in_range(self):
        report = load_report()
        rate = report.get("coverage", {}).get("coverage_rate")
        assert isinstance(rate, (int, float)), (
            f"coverage_rate must be numeric, got {type(rate)}"
        )
        assert 0 <= rate <= 100, (
            f"coverage_rate {rate} out of range [0, 100]"
        )


class TestHookCoverage:
    """Verify that both official plugin hooks and DB polling approach are documented."""

    def test_both_access_mechanisms_documented(self):
        """
        OpenCode offers TWO access mechanisms: (1) plugin hooks (official API)
        and (2) direct SQLite polling (unofficial, lazyagent approach).
        Both must be represented in the report or risks must explain why one was excluded.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])
        assert len(hook_types) >= 2, (
            f"Only {len(hook_types)} hook_type documented. "
            "OpenCode offers at minimum plugin hooks (tool.execute.*) and "
            "SQLite DB polling — both must be assessed."
        )

    def test_tool_execute_before_and_after_both_present(self):
        """
        Both tool.execute.before and tool.execute.after must be documented.
        They form a pair: before gives args, after gives output.
        Documenting only one gives incomplete tool call data.
        """
        report = load_report()
        hook_names = [h.get("name", "").lower() for h in report.get("hook_types", [])]

        before_present = any("before" in n for n in hook_names)
        after_present = any("after" in n for n in hook_names)

        assert before_present, (
            "tool.execute.before not documented. "
            "This hook provides tool arguments before execution."
        )
        assert after_present, (
            "tool.execute.after not documented. "
            "This hook provides tool output after execution."
        )

    def test_session_event_coverage(self):
        """
        Session lifecycle events must be covered.
        OpenCode has: session.created, session.updated, session.deleted,
        session.idle, session.error at minimum (all documented in official plugins page).
        """
        report = load_report()
        hook_names = " ".join(h.get("name", "").lower() for h in report.get("hook_types", []))

        session_covered = "session" in hook_names
        assert session_covered, (
            "No session lifecycle events documented. "
            "OpenCode has session.created/updated/deleted/idle/error in plugin API."
        )

    def test_needed_event_types_covers_secondsight_requirements(self):
        """
        needed_event_types must be at least 5 to cover SecondSight's minimum:
        tool_call_type, tool_arguments, tool_results, timestamps, session_lifecycle.
        """
        report = load_report()
        needed = report.get("coverage", {}).get("needed_event_types", 0)
        assert needed >= 5, (
            f"needed_event_types = {needed}, but SecondSight requires at least 5 data categories. "
            "Investigation understated requirements."
        )


class TestSecondSightNeedsMapping:
    """Verify SecondSight's required data points are explicitly mapped."""

    def test_each_hook_has_analysis_use_fields(self):
        """
        Every documented hook type MUST have at least one payload_field
        with a non-empty analysis_use value explaining how Phase 2 uses it.
        """
        report = load_report()
        for hook in report.get("hook_types", []):
            name = hook.get("name", "<unnamed>")
            has_analysis_use = any(
                field.get("analysis_use", "").strip()
                for field in hook.get("payload_fields", [])
            )
            assert has_analysis_use, (
                f"Hook '{name}' has no payload_field with analysis_use documented. "
                "Without this, the mapping from event data to SecondSight Analysis Layer "
                "is implicit — DC-1 risk."
            )

    def test_timestamp_coverage_addressed(self):
        """
        Timestamps (start, end) are required by SecondSight.
        Either a hook provides them or the DB polling approach does (time_created columns).
        This must be explicitly acknowledged.
        """
        report = load_report()

        # Check if any hook documents timing
        found_timestamp = any(
            any(kw in field.get("name", "").lower() or kw in field.get("analysis_use", "").lower()
                for kw in ["time", "timestamp", "start", "end", "duration", "created"])
            for hook in report.get("hook_types", [])
            for field in hook.get("payload_fields", [])
        )

        if not found_timestamp:
            # Must be in missing or risks
            all_text = (
                " ".join(str(r).lower() for r in report.get("risks", []))
                + " " + " ".join(str(m).lower() for m in report.get("missing", []))
            )
            assert any(kw in all_text for kw in ["time", "timestamp", "timing"]), (
                "Timestamps are required by SecondSight but not documented in any "
                "payload_field and not mentioned in risks/missing. Silently omitted."
            )

    def test_token_usage_addressed(self):
        """
        Token usage per call is required by SecondSight.
        OpenCode plugin hooks don't expose token data directly.
        This must appear in 'missing' or be documented in a DB-polling hook entry.
        """
        report = load_report()

        found_token = any(
            any(kw in field.get("name", "").lower()
                for kw in ["token", "usage", "cost"])
            for hook in report.get("hook_types", [])
            for field in hook.get("payload_fields", [])
        )

        if not found_token:
            all_text = (
                " ".join(str(m).lower() for m in report.get("missing", []))
                + " " + " ".join(str(r).lower() for r in report.get("risks", []))
            )
            assert "token" in all_text or "usage" in all_text or "cost" in all_text, (
                "Token usage is required by SecondSight but not documented in any payload "
                "and not in missing/risks. This gap is silently omitted."
            )

    def test_sub_agent_spawning_addressed(self):
        """
        Sub-agent spawning events are required by SecondSight for multi-agent tracking.
        OpenCode supports sub-agents via SubtaskPart and parent_id in the session table.
        This must be addressed — either as available or as missing.
        """
        report = load_report()

        hook_text = " ".join(h.get("name", "").lower() for h in report.get("hook_types", []))
        missing_text = " ".join(str(m).lower() for m in report.get("missing", []))
        all_text = hook_text + " " + missing_text + " " + str(report).lower()

        assert any(kw in all_text for kw in ["sub", "agent", "parent", "subtask", "spawn", "sidechain"]), (
            "Sub-agent spawning is required by SecondSight but not addressed anywhere in the report. "
            "OpenCode has parent_id in session table and SubtaskPart — this must be evaluated."
        )


class TestCoverageCalculationConsistency:
    """Verify coverage math is internally consistent."""

    def test_available_does_not_exceed_hook_type_count(self):
        report = load_report()
        coverage = report.get("coverage", {})
        available = coverage.get("available_event_types", 0)
        hook_count = len(report.get("hook_types", []))
        assert available <= hook_count, (
            f"available_event_types ({available}) > len(hook_types) ({hook_count}). "
            "Coverage claims more types than are documented in hook_types."
        )

    def test_risks_list_is_non_empty(self):
        """Every investigation must document at least one risk."""
        report = load_report()
        risks = report.get("risks", [])
        assert len(risks) >= 2, (
            f"Only {len(risks)} risk(s) documented. "
            "OpenCode investigation has at minimum: schema stability risk and "
            "plugin runtime (Bun/JS) constraint risk."
        )
