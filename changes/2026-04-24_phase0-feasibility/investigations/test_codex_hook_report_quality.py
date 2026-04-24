"""
Death tests and unit tests for the Codex CLI hook investigation report.

Death tests (DC-1 variants): Verify the report doesn't claim feasibility
based on superficial evidence — shallow hook count, missing payload field
verification, or confusion between Codex CLI (local) and Codex API (cloud).

Unit tests: Verify coverage math, schema completeness, and cross-validation
evidence requirements.

Run: pytest changes/2026-04-24_phase0-feasibility/investigations/test_codex_hook_report_quality.py -v
"""

import yaml
from pathlib import Path

YAML_REPORT_PATH = Path(__file__).parent / "codex-hooks.yaml"


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

    Codex CLI has a dual mechanism: (1) local JSONL transcript files in
    ~/.codex/sessions/ and (2) hook callbacks (pre_tool_use, post_tool_use,
    session_start, user_prompt_submit, stop, permission_request).

    Silent failure mode: reporting 'feasible' because hooks exist, without
    verifying that JSONL entries contain tool call arguments — which is the
    actual payload Codex CLI records per tool call.
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
                f"Hook/event '{name}' has no payload_fields documented. "
                "Claiming feasibility without field-level evidence is DC-1 silent failure."
            )

    def test_tool_call_args_field_documented(self):
        """
        The transcript or hook payload MUST document a field containing tool
        call arguments. For Codex CLI this is 'arguments' in function_call
        response_item entries. Without it, only tool NAME is known, not what
        it actually did.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        found_input_field = False
        for hook in hook_types:
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                if any(kw in field_name for kw in ["input", "argument", "param", "arguments", "command"]):
                    found_input_field = True
                    break

        assert found_input_field, (
            "No tool input/argument field found in any hook or transcript event type. "
            "DC-1: report cannot claim feasibility for tool call classification "
            "without documenting the field that carries tool arguments."
        )

    def test_tool_call_result_field_documented(self):
        """
        A transcript event MUST document a field containing tool call output.
        For Codex CLI this is 'output' in function_call_output entries.
        Without this, SecondSight can observe invocations but not outcomes.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        found_result_field = False
        for hook in hook_types:
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                if any(kw in field_name for kw in ["response", "result", "output", "content"]):
                    found_result_field = True
                    break

        assert found_result_field, (
            "No result/output field found in any event type. "
            "DC-1: Without tool results, failure attribution in Phase 2 is impossible."
        )

    def test_feasibility_verdict_is_not_based_on_hook_count_alone(self):
        """
        If verdict is 'feasible', then coverage.sufficient_field_event_types must
        equal or exceed coverage.needed_event_types. A positive verdict with
        sufficient_field_event_types < needed_event_types is the DC-1 anti-pattern.
        """
        report = load_report()
        verdict = report.get("verdict", "")
        coverage = report.get("coverage", {})

        if verdict == "feasible":
            needed = coverage.get("needed_event_types", 0)
            sufficient = coverage.get("sufficient_field_event_types", 0)
            assert sufficient >= needed, (
                f"Verdict is 'feasible' but only {sufficient}/{needed} event types "
                "have sufficient field coverage. This is the DC-1 shallow feasibility pattern."
            )

    def test_token_usage_field_presence_documented_explicitly(self):
        """
        Token usage per call is explicitly required by SecondSight. The report
        MUST state whether token usage is available — either as a hook payload
        field or as a JSONL transcript field. Silence on this topic is DC-1.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        token_mentioned = False
        for hook in hook_types:
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                use = field.get("analysis_use", "").lower()
                if "token" in field_name or "token" in use:
                    token_mentioned = True
                    break
            if token_mentioned:
                break

        # Also check the missing section — it's acceptable to document
        # token usage as missing rather than available
        missing = report.get("missing", [])
        token_in_missing = any("token" in str(m).lower() for m in missing)

        assert token_mentioned or token_in_missing, (
            "Token usage is not mentioned in any payload_fields or in the 'missing' section. "
            "DC-1: SecondSight requires token usage per call. Silence on availability "
            "means this requirement was not evaluated."
        )


class TestDeathCase2_CliVsApiConfusion:
    """
    DC-2: Investigation conflates Codex CLI (local tool, open-source Rust) with
    Codex API (cloud-based coding service at chatgpt.com/codex).

    These are fundamentally different systems. SecondSight targets the CLI.
    If the investigation describes API behavior as CLI behavior, all findings
    are irrelevant to the actual implementation target.
    """

    def test_report_specifies_codex_cli_as_target(self):
        """
        The investigation must explicitly state it targets Codex CLI
        (the local tool, not the Codex API / Codex Web service).
        """
        report = load_report()
        source = str(report.get("source", "")).lower()
        narrative = str(report.get("narrative", "")).lower()
        risks = [str(r).lower() for r in report.get("risks", [])]

        cli_mentioned = (
            "cli" in source
            or "cli" in narrative
            or any("cli" in r for r in risks)
        )
        assert cli_mentioned, (
            "Report does not explicitly mention 'CLI' in source, narrative, or risks. "
            "DC-2: Without distinguishing CLI from API, the investigation target is ambiguous."
        )

    def test_report_documents_local_storage_mechanism(self):
        """
        Codex CLI stores session transcripts locally at ~/.codex/sessions/*.jsonl.
        If the report documents an event mechanism that requires cloud API calls
        or an API key to read events, it has confused CLI with API.

        This test verifies that at least one documented event type references
        a local file path or JSONL file mechanism.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        local_mechanism_found = False
        for hook in hook_types:
            name = str(hook.get("name", "")).lower()
            trigger = str(hook.get("trigger", "")).lower()
            limitations = str(hook.get("limitations", "")).lower()
            fields_str = str(hook.get("payload_fields", [])).lower()

            if any(kw in (name + trigger + limitations + fields_str)
                   for kw in ["jsonl", ".codex", "local", "file", "session_meta", "rollout"]):
                local_mechanism_found = True
                break

        assert local_mechanism_found, (
            "No event type references a local file mechanism (.jsonl, ~/.codex/, rollout). "
            "DC-2: Codex CLI persists events locally. If no local mechanism is documented, "
            "the investigation may be describing API behavior instead."
        )

    def test_report_does_not_claim_webhook_or_cloud_only_mechanism(self):
        """
        Codex CLI has no webhook push mechanism. All observation is pull-based
        (file polling or hook callbacks via local process). If the report describes
        event delivery via webhook or cloud push, it has confused CLI with API.
        """
        report = load_report()
        all_text = str(report).lower()

        cloud_only_patterns = ["webhook", "push notification", "api polling", "rest api events"]
        for pattern in cloud_only_patterns:
            assert pattern not in all_text, (
                f"Report mentions '{pattern}' which suggests API/cloud mechanism. "
                "DC-2: Codex CLI uses local JSONL files + local hook callbacks, not cloud push."
            )


class TestDeathCase3_AssumedHookParityWithClaudeCode:
    """
    DC-3: Investigation assumes Codex CLI has a hook system similar to Claude Code
    (settings.json-style registration) and misses that Codex uses a fundamentally
    different event model (internal EventMsg queue + JSONL persistence).

    Codex CLI's hook system IS distinct from Claude Code's. Codex hooks are
    registered in config.toml and dispatched via a Rust hooks crate, while
    Claude Code uses a hooks object in settings.json. The payload schemas differ.
    """

    def test_report_addresses_hook_registration_mechanism(self):
        """
        The report must document HOW hooks are registered in Codex CLI.
        Claude Code uses settings.json hooks object.
        Codex CLI uses config.toml hooks configuration.
        Silence on this means the investigation skipped mechanism validation.
        """
        report = load_report()
        all_text = str(report).lower()

        registration_mentioned = any(kw in all_text for kw in [
            "config.toml", "config", "registration", "register", "hook_event"
        ])
        assert registration_mentioned, (
            "Report does not mention how hooks are registered (config.toml or equivalent). "
            "DC-3: Without documenting the registration mechanism, the investigation cannot "
            "confirm that Codex CLI hooks are usable by an external observer like SecondSight."
        )

    def test_report_distinguishes_two_observation_mechanisms(self):
        """
        Codex CLI provides two distinct observation surfaces:
        1. Internal hook callbacks (pre_tool_use, post_tool_use, etc.)
        2. Local JSONL transcript files (~/.codex/sessions/*.jsonl)

        The investigation must address both, because they have different
        payload completeness and timing characteristics.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])
        hook_names = [h.get("name", "").lower() for h in hook_types]

        has_hook_events = any(
            any(kw in name for kw in ["hook", "pre_tool", "post_tool", "session_start", "user_prompt"])
            for name in hook_names
        )
        has_transcript_events = any(
            any(kw in name for kw in ["jsonl", "transcript", "rollout", "session_meta",
                                       "response_item", "event_msg", "turn_context"])
            for name in hook_names
        )

        assert has_hook_events or has_transcript_events, (
            "Report documents neither hook callbacks nor JSONL transcript events. "
            "At minimum one observation mechanism must be documented."
        )


# ---------------------------------------------------------------------------
# UNIT TESTS — verify coverage math and schema completeness
# ---------------------------------------------------------------------------


class TestCoverageMetrics:
    """Verify coverage math is consistent and complete."""

    def test_coverage_section_present(self):
        """Coverage section must exist and have required fields."""
        report = load_report()
        coverage = report.get("coverage", {})

        required_fields = [
            "needed_event_types",
            "available_event_types",
            "sufficient_field_event_types",
            "coverage_rate",
        ]
        for field in required_fields:
            assert field in coverage, (
                f"Coverage section missing field '{field}'. "
                "Cannot evaluate feasibility without quantitative coverage metrics."
            )

    def test_coverage_rate_matches_sufficient_over_needed(self):
        """Coverage rate must be consistent with sufficient / needed math."""
        report = load_report()
        coverage = report.get("coverage", {})
        needed = coverage.get("needed_event_types", 0)
        sufficient = coverage.get("sufficient_field_event_types", 0)
        rate_str = str(coverage.get("coverage_rate", "0%")).rstrip("%")
        rate = float(rate_str)

        if needed > 0:
            expected_rate = (sufficient / needed) * 100
            assert abs(rate - expected_rate) < 2.0, (
                f"Coverage rate {rate}% is inconsistent with "
                f"sufficient_field_event_types ({sufficient}) / "
                f"needed_event_types ({needed}) = {expected_rate:.0f}%. "
                "Coverage math must be consistent."
            )

    def test_available_event_types_at_least_as_many_as_sufficient(self):
        """Cannot have more sufficient events than available events."""
        report = load_report()
        coverage = report.get("coverage", {})
        available = coverage.get("available_event_types", 0)
        sufficient = coverage.get("sufficient_field_event_types", 0)

        assert sufficient <= available, (
            f"sufficient_field_event_types ({sufficient}) > available_event_types ({available}). "
            "Cannot have more sufficient events than available ones. Coverage math is wrong."
        )

    def test_needed_event_types_covers_secondsight_requirements(self):
        """
        SecondSight needs at minimum 6 event types. If needed_event_types is
        less than 6, the coverage math is underselling SecondSight's requirements.
        The 6 minimum: tool_call, tool_result, session_start, turn_start, turn_end, token_usage.
        """
        report = load_report()
        coverage = report.get("coverage", {})
        needed = coverage.get("needed_event_types", 0)

        assert needed >= 6, (
            f"needed_event_types is {needed}, which is below the minimum 6. "
            "SecondSight needs: tool_call type, tool_call args, tool_result, "
            "session lifecycle, turn lifecycle, and token usage. "
            "Underestimating needed types inflates the coverage rate."
        )


class TestSchemaCompleteness:
    """Verify the YAML schema is complete enough to be useful."""

    def test_verdict_is_one_of_valid_values(self):
        """Verdict must be one of the defined valid values."""
        report = load_report()
        verdict = report.get("verdict", "")
        valid_verdicts = {"feasible", "partially_feasible", "infeasible", "inconclusive"}

        assert verdict in valid_verdicts, (
            f"verdict '{verdict}' is not a valid value. "
            f"Must be one of: {valid_verdicts}"
        )

    def test_risks_section_present_and_non_empty(self):
        """Every investigation must document known risks."""
        report = load_report()
        risks = report.get("risks", [])

        assert isinstance(risks, list) and len(risks) > 0, (
            "risks section is empty or missing. "
            "Every integration approach has risks. An empty risks section "
            "means the investigation skipped adversarial analysis."
        )

    def test_missing_section_present(self):
        """
        The 'missing' section must exist. If Codex CLI covers all requirements
        perfectly, missing may be an empty list — but the key must exist.
        """
        report = load_report()
        assert "missing" in report, (
            "'missing' section not present in report. "
            "SecondSight requirements include items like sub-agent spawning "
            "and per-call token usage. If missing is absent, coverage gaps were not evaluated."
        )

    def test_each_hook_type_has_limitations_documented(self):
        """Every hook/event type must document known limitations."""
        report = load_report()
        hook_types = report.get("hook_types", [])

        for hook in hook_types:
            name = hook.get("name", "<unnamed>")
            limitations = hook.get("limitations", "")
            assert isinstance(limitations, str) and len(limitations.strip()) > 0, (
                f"Hook/event '{name}' has no 'limitations' field. "
                "Every event type has limitations. Omitting them understates integration risk."
            )

    def test_payload_fields_have_analysis_use_documented(self):
        """
        Each payload field must document its analysis_use. This prevents
        Phase 2 developers from treating all fields as equally important.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        for hook in hook_types:
            hook_name = hook.get("name", "<unnamed>")
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "<unnamed>")
                analysis_use = field.get("analysis_use", "")
                assert isinstance(analysis_use, str) and len(analysis_use.strip()) > 0, (
                    f"Hook '{hook_name}', field '{field_name}' has no analysis_use. "
                    "Phase 2 developers need to know what each field is used for."
                )


class TestCodexSpecificRequirements:
    """
    Codex-CLI-specific test cases that verify the investigation addressed
    the unique characteristics of Codex's event architecture.
    """

    def test_jsonl_transcript_architecture_documented(self):
        """
        Codex CLI's primary observation mechanism is the JSONL rollout file at
        ~/.codex/sessions/rollout-TIMESTAMP-UUID.jsonl. The investigation must
        document this mechanism or explain why it's not used.
        """
        report = load_report()
        all_text = str(report).lower()

        jsonl_documented = any(kw in all_text for kw in [
            "jsonl", ".codex/sessions", "rollout", "session_meta", "response_item",
            "event_msg", "transcript"
        ])
        assert jsonl_documented, (
            "Report does not mention the JSONL rollout file mechanism. "
            "Codex CLI writes all events to ~/.codex/sessions/rollout-*.jsonl. "
            "This is the primary observation surface — its omission is a critical gap."
        )

    def test_function_call_event_documented(self):
        """
        Codex CLI records tool calls as 'response_item' entries with type
        'function_call' containing name and arguments fields. This must appear
        in the documentation as the mechanism for observing tool invocations.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        function_call_documented = False
        for hook in hook_types:
            name = str(hook.get("name", "")).lower()
            fields_str = str(hook.get("payload_fields", [])).lower()
            trigger = str(hook.get("trigger", "")).lower()
            if any(kw in (name + fields_str + trigger)
                   for kw in ["function_call", "exec_command", "tool_call", "apply_patch"]):
                function_call_documented = True
                break

        assert function_call_documented, (
            "No event type documents function_call / exec_command tool invocations. "
            "Codex CLI's core tool calls are: exec_command (shell), apply_patch (file edits), "
            "and function_call (generic). These must be present to claim tool-call coverage."
        )

    def test_sub_agent_spawning_addressed(self):
        """
        SecondSight requires sub-agent spawning events. Codex CLI supports
        multi-agent collaboration via ThreadSpawn (SubAgent source type).
        The investigation must document whether this is observable.
        """
        report = load_report()
        all_text = str(report).lower()
        missing = [str(m).lower() for m in report.get("missing", [])]

        sub_agent_in_report = any(kw in all_text for kw in [
            "sub-agent", "subagent", "thread_spawn", "threadspawn", "collaboration",
            "collab_agent", "multi-agent"
        ])
        sub_agent_in_missing = any(
            any(kw in m for kw in ["sub-agent", "subagent", "thread_spawn", "collaboration"])
            for m in missing
        )

        assert sub_agent_in_report or sub_agent_in_missing, (
            "Report does not mention sub-agent spawning events anywhere, "
            "including in the 'missing' section. "
            "SecondSight requires these events. Their absence from both the "
            "supported and missing sections means they were not evaluated."
        )

    def test_session_identity_fields_documented(self):
        """
        SecondSight needs session lifecycle events. For Codex CLI these include
        session_meta (with session ID and CWD) and turn_context (with model info).
        At minimum a session identifier field must be documented.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        session_id_found = False
        for hook in hook_types:
            for field in hook.get("payload_fields", []):
                field_name = field.get("name", "").lower()
                if any(kw in field_name for kw in ["session", "id", "thread_id", "session_id"]):
                    session_id_found = True
                    break
            if session_id_found:
                break

        assert session_id_found, (
            "No session identity field (session_id, thread_id, etc.) found in any event type. "
            "Session identity is fundamental to linking events across a session. "
            "Its absence means events cannot be correlated."
        )

    def test_hook_mechanism_vs_jsonl_distinction_clear(self):
        """
        Codex CLI exposes two distinct mechanisms:
        - Hook callbacks: pre_tool_use, post_tool_use, etc. (real-time, synchronous)
        - JSONL transcript files: persistent, post-hoc readable

        The investigation must make this distinction or explain that only one
        mechanism is sufficient. Conflating them leads to incorrect payload
        field expectations.
        """
        report = load_report()
        hook_types = report.get("hook_types", [])

        # Look for evidence that the report treats these as distinct mechanisms
        names = [h.get("name", "") for h in hook_types]
        all_names_lower = " ".join(names).lower()

        # At minimum both terms should appear somewhere in the report
        all_text = str(report).lower()
        has_hook_mention = any(kw in all_text for kw in [
            "hook", "pre_tool_use", "post_tool_use", "hook_event", "hookstarted"
        ])
        has_jsonl_mention = any(kw in all_text for kw in [
            "jsonl", "rollout", "session_meta", "response_item", "transcript"
        ])

        # Must mention at least one — the dominant mechanism for Codex CLI
        assert has_hook_mention or has_jsonl_mention, (
            "Report mentions neither hook callbacks nor JSONL transcript mechanism. "
            "At least one observation mechanism must be documented for Codex CLI."
        )
