"""
Tests for the Unified Event Schema POC (Task 7).

Test organization:
1. DEATH TESTS (test_death_*) -- test silent failure paths FIRST
   These verify the schema detects and rejects anti-patterns:
   - DC-3: > 50% untyped metadata fields per agent
   - False unification: fields defined for one agent always empty for others
   - Schema cannot represent real events from reference data
   - Schema version has no migration path

2. UNIT TESTS (test_*) -- test correct behavior
   These verify the schema works as intended:
   - Schema validates sample events from all three agents
   - typed_field_percentage >= 50% per agent
   - Schema produces valid JSON Schema export
   - Schema handles Codex double-parsing correctly
"""

from __future__ import annotations

import json
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


# ============================================================================
# DEATH TESTS -- Silent failure path testing
# ============================================================================


class TestDeathUntypedMetadata:
    """DC-3: Schema hides incompatibility behind untyped metadata fields.

    Detection criterion: typed_field_percentage per agent must be >= 50%.
    If a schema "validates" events but shoves most data into dict[str, Any]
    metadata bags, it has failed silently.
    """

    def test_death_claude_code_typed_field_percentage(
        self,
        claude_code_pre_tool_use_event,
        claude_code_post_tool_use_event,
        claude_code_stop_event,
        claude_code_jsonl_assistant_event,
    ):
        """Schema must represent >= 50% of Claude Code event data in typed fields.

        A schema that puts tool_name, tool_input, tool_response, session_id, etc.
        into metadata: dict fails this test.
        """
        from secondsight.poc.event_schema import (
            normalize_event,
            compute_typed_field_percentage,
        )

        events = [
            claude_code_pre_tool_use_event,
            claude_code_post_tool_use_event,
            claude_code_stop_event,
            claude_code_jsonl_assistant_event,
        ]

        percentages = []
        for event_data in events:
            event = normalize_event(event_data["agent"], event_data["raw"])
            pct = compute_typed_field_percentage(event)
            percentages.append(pct)

        avg_pct = sum(percentages) / len(percentages)
        assert avg_pct >= 50.0, (
            f"Claude Code typed field percentage is {avg_pct:.1f}%, must be >= 50%. "
            f"Per-event: {percentages}. "
            f"Schema is hiding data in untyped metadata fields (DC-3)."
        )

    def test_death_opencode_typed_field_percentage(
        self,
        opencode_tool_execute_before_event,
        opencode_tool_execute_after_event,
        opencode_session_created_event,
        opencode_db_part_tool_event,
    ):
        """Schema must represent >= 50% of OpenCode event data in typed fields."""
        from secondsight.poc.event_schema import (
            normalize_event,
            compute_typed_field_percentage,
        )

        events = [
            opencode_tool_execute_before_event,
            opencode_tool_execute_after_event,
            opencode_session_created_event,
            opencode_db_part_tool_event,
        ]

        percentages = []
        for event_data in events:
            event = normalize_event(event_data["agent"], event_data["raw"])
            pct = compute_typed_field_percentage(event)
            percentages.append(pct)

        avg_pct = sum(percentages) / len(percentages)
        assert avg_pct >= 50.0, (
            f"OpenCode typed field percentage is {avg_pct:.1f}%, must be >= 50%. "
            f"Per-event: {percentages}. "
            f"Schema is hiding data in untyped metadata fields (DC-3)."
        )

    def test_death_codex_typed_field_percentage(
        self,
        codex_session_meta_event,
        codex_function_call_event,
        codex_function_call_output_event,
        codex_token_count_event,
    ):
        """Schema must represent >= 50% of Codex event data in typed fields."""
        from secondsight.poc.event_schema import (
            normalize_event,
            compute_typed_field_percentage,
        )

        events = [
            codex_session_meta_event,
            codex_function_call_event,
            codex_function_call_output_event,
            codex_token_count_event,
        ]

        percentages = []
        for event_data in events:
            event = normalize_event(event_data["agent"], event_data["raw"])
            pct = compute_typed_field_percentage(event)
            percentages.append(pct)

        avg_pct = sum(percentages) / len(percentages)
        assert avg_pct >= 50.0, (
            f"Codex typed field percentage is {avg_pct:.1f}%, must be >= 50%. "
            f"Per-event: {percentages}. "
            f"Schema is hiding data in untyped metadata fields (DC-3)."
        )


class TestDeathFalseUnification:
    """Schema defines fields for Agent A that are always empty for Agent B and C.

    A schema with exit_code: Optional[int] that is ALWAYS None for Claude Code
    and OpenCode creates a false impression of unification. The typed field
    technically exists but never carries data for most agents.
    """

    def test_death_no_agent_exclusive_required_fields(self):
        """No typed field should be populated for exactly one agent but None for all others.

        Checks across MULTIPLE event types per agent to detect fields that
        are structurally single-agent (never populated for the other two).

        Fields like timestamp and cwd are universally relevant even if not
        every event source provides them, so they are excluded from this check.
        The concern is fields that are CONCEPTUALLY single-agent but placed
        in the shared typed schema (false unification).
        """
        from secondsight.poc.event_schema import (
            normalize_event,
            get_schema_field_names,
        )

        # Use the richest event from each agent to maximize field population.
        # Multiple events per agent to ensure we see the broadest field set.
        agent_events = {
            "claude_code": [
                {
                    "session_id": "s1",
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "t1",
                    "tool_input": {"command": "ls"},
                    "tool_response": {"stdout": "file.txt", "stderr": ""},
                    "cwd": "/tmp",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "model": "claude-sonnet-4-6",
                        "id": "msg1",
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0},
                    },
                    "sessionId": "s1",
                    "timestamp": "2026-04-24T10:00:00Z",
                    "cwd": "/tmp",
                    "version": "2.1.85",
                },
            ],
            "opencode": [
                {
                    "input": {
                        "tool": "bash",
                        "sessionID": "s2",
                        "callID": "c1",
                        "args": {"command": "ls"},
                    },
                    "output": {
                        "output": "file.txt",
                        "title": "bash: ls",
                        "metadata": {},
                    },
                },
                {
                    "source": "db_polling",
                    "part": {
                        "data": {
                            "type": "tool",
                            "tool": "bash",
                            "callID": "c2",
                            "state": {
                                "input": {"command": "ls"},
                                "output": "file.txt",
                                "time": {"start": 1714000100000, "end": 1714000101500},
                            },
                        },
                    },
                    "session_id": "s2",
                },
            ],
            "codex": [
                {
                    "timestamp": "2026-03-28T11:26:19.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": "file.txt",
                    },
                },
                {
                    "session_id": "s3",
                    "cwd": "/tmp",
                    "triggered_at": "2026-03-28T11:26:20.500Z",
                    "hook_event": {
                        "event_type": "after_tool_use",
                        "turn_id": "t1",
                        "call_id": "c1",
                        "tool_name": "exec_command",
                        "tool_kind": "function",
                        "tool_input": {"arguments": '{"cmd":"ls"}'},
                        "executed": True,
                        "success": True,
                        "duration_ms": 500,
                        "mutating": False,
                        "sandbox": "read-only",
                        "output_preview": "file.txt",
                    },
                },
            ],
        }

        shared_fields = get_schema_field_names()

        # Universally relevant context fields: meaningful for all agents
        # even if not every event source carries them. These are NOT
        # false unification -- they are legitimately shared schema fields.
        context_fields = {"timestamp", "cwd", "session_id"}

        # For each field, track whether ANY event from each agent populates it
        field_population: dict[str, set[str]] = {f: set() for f in shared_fields}
        for agent_name, events in agent_events.items():
            for raw in events:
                event = normalize_event(agent_name, raw)
                for field_name in shared_fields:
                    val = getattr(event, field_name, None)
                    if val is not None:
                        field_population[field_name].add(agent_name)

        # Find fields populated by exactly 1 agent, excluding context fields
        single_agent_fields = {
            name: agents
            for name, agents in field_population.items()
            if len(agents) == 1
            and name not in context_fields
            and name != "agent_metadata"
        }

        # Allow at most 2 single-agent fields in shared schema
        # (some asymmetry is expected; more than 2 signals false unification)
        assert len(single_agent_fields) <= 2, (
            f"False unification detected: {len(single_agent_fields)} shared typed fields "
            f"are populated by only one agent: {single_agent_fields}. "
            f"These should be in agent_metadata, not shared typed fields."
        )


class TestDeathCannotRepresentRealEvents:
    """Schema cannot represent a real event from reference_opensoure sample data.

    If the schema fails to parse actual event data from the reference
    projects, it is useless for its stated purpose.
    """

    def test_death_codex_real_jsonl_event(self):
        """Schema must parse the exact Codex JSONL format from lazyagent process_test.go."""
        from secondsight.poc.event_schema import normalize_event

        # This is the exact format from reference_opensoure/lazyagent/internal/codex/process_test.go line 38-44
        real_codex_events = [
            {
                "timestamp": "2026-03-28T11:26:17.785Z",
                "type": "session_meta",
                "payload": {
                    "id": "019d3431-8669-7603-be71-7079fa555f4a",
                    "cwd": "/tmp/project",
                    "cli_version": "0.116.0",
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-03-28T11:26:19.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    # CRITICAL: arguments is a JSON string, not a dict
                    "arguments": '{"cmd":"rg codex"}',
                },
            },
            {
                "timestamp": "2026-03-28T11:26:20.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "ok",
                },
            },
            {
                "timestamp": "2026-03-28T11:26:21.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1200,
                            "cached_input_tokens": 300,
                            "output_tokens": 500,
                            "reasoning_output_tokens": 100,
                        },
                    },
                },
            },
        ]

        for raw_event in real_codex_events:
            event = normalize_event("codex", raw_event)
            assert event is not None, (
                f"Failed to parse real Codex JSONL event: {raw_event['type']}"
            )
            assert event.agent_type == "codex"

    def test_death_codex_double_parse_arguments(self):
        """Schema must correctly handle Codex's JSON-encoded-string arguments.

        Codex function_call.arguments is '{"cmd":"rg codex"}' (a string),
        not {"cmd": "rg codex"} (a dict). The schema must parse this into
        a typed field, not dump it into metadata.
        """
        from secondsight.poc.event_schema import normalize_event

        raw = {
            "timestamp": "2026-03-28T11:26:19.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"rg codex"}',
            },
        }

        event = normalize_event("codex", raw)
        # tool_args must be a dict (parsed from the JSON string), not the raw string
        assert event.tool_args is not None, "tool_args should not be None for function_call"
        assert isinstance(event.tool_args, dict), (
            f"tool_args should be dict (parsed from JSON string), got {type(event.tool_args)}: "
            f"{event.tool_args!r}"
        )
        assert event.tool_args.get("cmd") == "rg codex"


class TestDeathSchemaVersioning:
    """Schema version has no migration path -- old events become unreadable."""

    def test_death_schema_has_version_field(self):
        """Schema must include a version identifier so old events can be distinguished."""
        from secondsight.poc.event_schema import SCHEMA_VERSION, SecondSightEvent

        assert SCHEMA_VERSION is not None
        assert isinstance(SCHEMA_VERSION, str)
        assert len(SCHEMA_VERSION) > 0

        # The schema version should be embeddable in serialized events
        # so that readers can detect version mismatches
        assert hasattr(SecondSightEvent, "schema_version"), (
            "SecondSightEvent must have a schema_version field for migration detection"
        )

    def test_death_schema_version_in_json_export(self):
        """JSON Schema export must include version so validators can detect mismatches."""
        schema_path = Path(__file__).parent.parent.parent / "src" / "secondsight" / "poc" / "event_schema.json"
        assert schema_path.exists(), f"JSON Schema file not found at {schema_path}"

        with open(schema_path) as f:
            schema = json.load(f)

        # Schema must include version in a discoverable location
        assert "version" in schema or "$comment" in schema or "title" in schema, (
            "JSON Schema export must include version information"
        )
        # Check for actual version string
        schema_text = json.dumps(schema)
        from secondsight.poc.event_schema import SCHEMA_VERSION
        assert SCHEMA_VERSION in schema_text, (
            f"JSON Schema export must reference schema version {SCHEMA_VERSION}"
        )


# ============================================================================
# UNIT TESTS -- Correct behavior testing
# ============================================================================


class TestSchemaValidation:
    """Schema validates sample events from all three agents."""

    def test_claude_code_pre_tool_use(self, claude_code_pre_tool_use_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_pre_tool_use_event["agent"],
            claude_code_pre_tool_use_event["raw"],
        )
        assert event.agent_type == "claude_code"
        assert event.event_type == "tool_call_start"
        assert event.session_id == "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0"
        assert event.tool_name == "Bash"
        assert event.tool_args == {"command": "git status"}

    def test_claude_code_post_tool_use(self, claude_code_post_tool_use_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_post_tool_use_event["agent"],
            claude_code_post_tool_use_event["raw"],
        )
        assert event.agent_type == "claude_code"
        assert event.event_type == "tool_call_end"
        assert event.tool_name == "Bash"
        assert event.tool_result is not None

    def test_claude_code_stop(self, claude_code_stop_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_stop_event["agent"],
            claude_code_stop_event["raw"],
        )
        assert event.agent_type == "claude_code"
        assert event.event_type == "turn_end"

    def test_claude_code_jsonl_assistant(self, claude_code_jsonl_assistant_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_jsonl_assistant_event["agent"],
            claude_code_jsonl_assistant_event["raw"],
        )
        assert event.agent_type == "claude_code"
        assert event.event_type == "agent_response"
        assert event.token_usage is not None
        assert event.token_usage.input_tokens == 1500
        assert event.token_usage.output_tokens == 200

    def test_opencode_tool_execute_before(self, opencode_tool_execute_before_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            opencode_tool_execute_before_event["agent"],
            opencode_tool_execute_before_event["raw"],
        )
        assert event.agent_type == "opencode"
        assert event.event_type == "tool_call_start"
        assert event.tool_name == "bash"
        assert event.tool_args == {"command": "ls -la"}

    def test_opencode_tool_execute_after(self, opencode_tool_execute_after_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            opencode_tool_execute_after_event["agent"],
            opencode_tool_execute_after_event["raw"],
        )
        assert event.agent_type == "opencode"
        assert event.event_type == "tool_call_end"
        assert event.tool_result is not None

    def test_opencode_session_created(self, opencode_session_created_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            opencode_session_created_event["agent"],
            opencode_session_created_event["raw"],
        )
        assert event.agent_type == "opencode"
        assert event.event_type == "session_start"
        assert event.session_id == "8c5d2e1a-f4b6-4c7d-8e9f-0a1b2c3d4e5f"

    def test_codex_session_meta(self, codex_session_meta_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_session_meta_event["agent"],
            codex_session_meta_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "session_start"
        assert event.session_id == "019d3431-8669-7603-be71-7079fa555f4a"

    def test_codex_function_call(self, codex_function_call_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_function_call_event["agent"],
            codex_function_call_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "tool_call_start"
        assert event.tool_name == "exec_command"
        # Arguments must be parsed from JSON string to dict
        assert isinstance(event.tool_args, dict)

    def test_codex_function_call_output(self, codex_function_call_output_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_function_call_output_event["agent"],
            codex_function_call_output_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "tool_call_end"
        assert event.tool_result is not None

    def test_codex_token_count(self, codex_token_count_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_token_count_event["agent"],
            codex_token_count_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "token_usage_report"
        assert event.token_usage is not None
        assert event.token_usage.input_tokens == 400
        assert event.token_usage.is_cumulative is False

    def test_codex_user_message(self, codex_user_message_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_user_message_event["agent"],
            codex_user_message_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "user_prompt"
        assert event.content is not None
        assert "please add codex" in event.content

    def test_codex_task_complete(self, codex_task_complete_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_task_complete_event["agent"],
            codex_task_complete_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "turn_end"


class TestSchemaJsonExport:
    """JSON Schema export is valid and comprehensive."""

    def test_json_schema_file_exists(self):
        schema_path = Path(__file__).parent.parent.parent / "src" / "secondsight" / "poc" / "event_schema.json"
        assert schema_path.exists(), f"JSON Schema not found at {schema_path}"

    def test_json_schema_is_valid_json(self):
        schema_path = Path(__file__).parent.parent.parent / "src" / "secondsight" / "poc" / "event_schema.json"
        with open(schema_path) as f:
            schema = json.load(f)
        assert isinstance(schema, dict)
        assert "type" in schema or "$schema" in schema

    def test_json_schema_includes_event_types(self):
        schema_path = Path(__file__).parent.parent.parent / "src" / "secondsight" / "poc" / "event_schema.json"
        with open(schema_path) as f:
            schema = json.load(f)
        schema_text = json.dumps(schema)
        # Must include the unified event types
        for event_type in ["tool_call_start", "tool_call_end", "session_start", "turn_end"]:
            assert event_type in schema_text, (
                f"JSON Schema must define event type '{event_type}'"
            )


class TestSchemaEventTypes:
    """Schema event types cover all required categories from PRD section 6.1."""

    def test_event_types_cover_requirements(self):
        """PRD requires: tool calls, session lifecycle, user prompts, agent responses."""
        from secondsight.poc.event_schema import EventType

        required_categories = {
            "tool_call_start",   # tool calls (pre)
            "tool_call_end",     # tool calls (post)
            "session_start",     # session lifecycle
            "session_end",       # session lifecycle
            "turn_end",          # session lifecycle (turn level)
            "user_prompt",       # user prompts
            "agent_response",    # agent responses
            "token_usage_report",  # token tracking
            "subagent_start",    # sub-agent lifecycle
            "subagent_end",      # sub-agent lifecycle
            "error",             # error events
        }

        actual_types = {e.value for e in EventType}
        missing = required_categories - actual_types
        assert not missing, (
            f"Schema is missing required event types: {missing}"
        )


class TestSchemaSerializationRoundTrip:
    """Events can be serialized to dict/JSON and deserialized back."""

    def test_roundtrip_claude_code(self, claude_code_pre_tool_use_event):
        from secondsight.poc.event_schema import normalize_event, event_to_dict, event_from_dict

        event = normalize_event(
            claude_code_pre_tool_use_event["agent"],
            claude_code_pre_tool_use_event["raw"],
        )
        as_dict = event_to_dict(event)
        restored = event_from_dict(as_dict)

        assert restored.agent_type == event.agent_type
        assert restored.event_type == event.event_type
        assert restored.session_id == event.session_id
        assert restored.tool_name == event.tool_name

    def test_roundtrip_codex(self, codex_function_call_event):
        from secondsight.poc.event_schema import normalize_event, event_to_dict, event_from_dict

        event = normalize_event(
            codex_function_call_event["agent"],
            codex_function_call_event["raw"],
        )
        as_dict = event_to_dict(event)
        restored = event_from_dict(as_dict)

        assert restored.agent_type == event.agent_type
        assert restored.tool_name == event.tool_name
        assert restored.tool_args == event.tool_args

    def test_serialized_json_is_valid(self, claude_code_post_tool_use_event):
        from secondsight.poc.event_schema import normalize_event, event_to_dict

        event = normalize_event(
            claude_code_post_tool_use_event["agent"],
            claude_code_post_tool_use_event["raw"],
        )
        as_dict = event_to_dict(event)
        # Must be JSON-serializable
        json_str = json.dumps(as_dict)
        assert json.loads(json_str) == as_dict


class TestSchemaPhase2Support:
    """Schema supports Phase 2 action classification fields."""

    def test_action_classification_field_exists(self):
        """Schema must have a field for Phase 2 classification
        (Aligned/Wasteful/Divergent/Exploratory/Premature/Over-verified).
        """
        from secondsight.poc.event_schema import SecondSightEvent

        assert hasattr(SecondSightEvent, "action_classification"), (
            "SecondSightEvent must have action_classification field for Phase 2"
        )

    def test_action_classification_is_optional(self, claude_code_pre_tool_use_event):
        """action_classification should be None at creation (populated in Phase 2)."""
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_pre_tool_use_event["agent"],
            claude_code_pre_tool_use_event["raw"],
        )
        assert event.action_classification is None


class TestSchemaSubagentSupport:
    """Schema supports sub-agent events from Claude Code."""

    def test_subagent_start_event(self, claude_code_subagent_start_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_subagent_start_event["agent"],
            claude_code_subagent_start_event["raw"],
        )
        assert event.event_type == "subagent_start"
        assert event.session_id == "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0"


class TestSchemaTokenUsageVariants:
    """Schema handles different token usage granularities across agents."""

    def test_claude_code_per_message_tokens(self, claude_code_jsonl_assistant_event):
        """Claude Code provides per-message token usage via JSONL."""
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            claude_code_jsonl_assistant_event["agent"],
            claude_code_jsonl_assistant_event["raw"],
        )
        assert event.token_usage is not None
        assert event.token_usage.input_tokens == 1500
        assert event.token_usage.output_tokens == 200
        assert event.token_usage.cache_read_tokens == 500

    def test_codex_per_turn_tokens(self, codex_token_count_event):
        """Codex prefers last_token_usage (per-turn delta) when available."""
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_token_count_event["agent"],
            codex_token_count_event["raw"],
        )
        assert event.token_usage is not None
        assert event.token_usage.input_tokens == 400
        assert event.token_usage.output_tokens == 150
        assert event.token_usage.is_cumulative is False

    def test_opencode_db_message_tokens(self, opencode_db_message_event):
        """OpenCode provides per-message tokens via SQLite DB polling."""
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            opencode_db_message_event["agent"],
            opencode_db_message_event["raw"],
        )
        assert event.token_usage is not None
        assert event.token_usage.input_tokens == 2000
        assert event.token_usage.output_tokens == 150


class TestSchemaOpenCodeDbEvents:
    """Schema handles OpenCode DB polling events (unofficial mechanism)."""

    def test_opencode_db_part_tool(self, opencode_db_part_tool_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            opencode_db_part_tool_event["agent"],
            opencode_db_part_tool_event["raw"],
        )
        assert event.agent_type == "opencode"
        assert event.tool_name == "read"
        assert event.tool_args is not None
        assert event.duration_ms is not None
        assert event.duration_ms == 1500  # end - start = 1500ms

    def test_opencode_db_message(self, opencode_db_message_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            opencode_db_message_event["agent"],
            opencode_db_message_event["raw"],
        )
        assert event.agent_type == "opencode"
        assert event.event_type == "agent_response"
        assert event.token_usage is not None


class TestSchemaCodexHookCallback:
    """Schema handles Codex hook callback events (Surface 2)."""

    def test_codex_post_tool_use_hook(self, codex_post_tool_use_hook_event):
        from secondsight.poc.event_schema import normalize_event

        event = normalize_event(
            codex_post_tool_use_hook_event["agent"],
            codex_post_tool_use_hook_event["raw"],
        )
        assert event.agent_type == "codex"
        assert event.event_type == "tool_call_end"
        assert event.tool_name == "exec_command"
        assert event.duration_ms == 1500
        # Codex hook provides explicit success field
        assert event.success is True
