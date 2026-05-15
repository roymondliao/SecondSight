"""Death tests and unit tests for AnalysisOutput pydantic contract (Task 2).

Execution order (samsara): death tests first, then unit tests.

Death tests: silent failure paths — cases where a bad value MUST raise
ValidationError. If any death test passes before the implementation exists,
it means pydantic isn't enforcing the contract.

Unit tests: happy-path construction, round-trip serialization, cross-field
invariants on valid inputs.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

# -----------------------------------------------------------------------
# DEATH TESTS (Step 1) — these MUST fail before implementation exists.
# All of these test that ValidationError is raised; if AnalysisOutput is
# not defined or defined too permissively, these tests catch it.
# -----------------------------------------------------------------------


class TestDeathCases:
    """Tests targeting silent failure paths. Write before implementation."""

    def test_missing_schema_version_raises(self) -> None:
        """DC2: JSON missing required field 'schema_version' → ValidationError."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_future_schema_version_raises(self) -> None:
        """DC2: schema_version='2.0' (unsupported future) → ValidationError.
        Only '1.0' is accepted via Literal."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "2.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_cli_mode_without_cli_agent_raises(self) -> None:
        """DC2 cross-field invariant: dispatched_via='cli' but cli_agent=None → ValidationError."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "cli",
            "cli_agent": None,
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_sdk_mode_without_primary_model_raises(self) -> None:
        """DC2 cross-field invariant: dispatched_via='sdk' but primary_model=None → ValidationError."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": None,
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_cli_mode_with_primary_model_raises(self) -> None:
        """DC2 cross-field invariant: dispatched_via='cli' with primary_model set → ValidationError.
        primary_model must be None for cli mode."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "cli",
            "cli_agent": "claude_code",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_sdk_mode_with_cli_agent_raises(self) -> None:
        """DC2 cross-field invariant: dispatched_via='sdk' with cli_agent set → ValidationError.
        cli_agent must be None for sdk mode."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
            "cli_agent": "claude_code",
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_retry_count_negative_raises(self) -> None:
        """DC2: retry_count=-1 → ValidationError (ge=0 constraint)."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
            "retry_count": -1,
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_retry_count_above_cap_raises(self) -> None:
        """DC2: retry_count=6 → ValidationError (Phase 1 hard cap = 5)."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
            "retry_count": 6,
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_extra_field_raises(self) -> None:
        """DC2 strict mode: extra unknown field 'hallucination' → ValidationError.
        model_config = ConfigDict(extra='forbid') must be enforced."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
            "hallucination": "injected by bad caller",
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_invalid_status_raises(self) -> None:
        """status must be Literal['success', 'failure', 'unknown'] — other values rejected."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "pending",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_unknown_status_with_cli_dispatch_and_no_cli_agent_raises(self) -> None:
        """Decision A death test: unknown status does NOT relax cross-field invariants.
        dispatched_via='cli' without cli_agent MUST still raise — even when status='unknown'.
        Pins Decision A: a future early-out for 'unknown' would silently break this."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "unknown",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "cli",
            "cli_agent": None,
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_unknown_status_with_sdk_dispatch_and_no_primary_model_raises(self) -> None:
        """Decision A death test: unknown status does NOT relax cross-field invariants.
        dispatched_via='sdk' without primary_model MUST still raise — even when status='unknown'.
        Pins Decision A: a future early-out for 'unknown' would silently break this."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "unknown",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": None,
        }
        with pytest.raises(ValidationError):
            AnalysisOutput.model_validate(raw)

    def test_dc4_failure_with_fallback_used_requires_error_details(self) -> None:
        """DC4 death test: status='failure' + dispatched_via='sdk' + fallback_used=True
        MUST have error_details. None is rejected — the MUST in the docstring must be
        enforced by the validator, not just prose."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "failure",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
            "fallback_used": True,
            "error_details": None,
        }
        with pytest.raises(ValidationError, match="DC4"):
            AnalysisOutput.model_validate(raw)

    def test_dc4_failure_with_fallback_used_requires_both_keys(self) -> None:
        """DC4 death test: status='failure' + dispatched_via='sdk' + fallback_used=True
        + error_details missing 'fallback_error' key MUST raise.
        Both 'primary_error' and 'fallback_error' are required for DC4."""
        from secondsight.analysis.output import AnalysisOutput

        raw = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "failure",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
            "fallback_used": True,
            "error_details": {"primary_error": "AnthropicError: 529"},
        }
        with pytest.raises(ValidationError, match="fallback_error"):
            AnalysisOutput.model_validate(raw)


# -----------------------------------------------------------------------
# UNIT TESTS (Step 3) — happy-path and edge-case construction.
# -----------------------------------------------------------------------


class TestAnalysisOutputConstruction:
    """Happy-path: construct valid AnalysisOutput instances and verify field access."""

    def _sdk_payload(self, **overrides: object) -> dict:
        """Minimal valid SDK-mode payload."""
        base: dict = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        base.update(overrides)
        return base

    def _cli_payload(self, **overrides: object) -> dict:
        """Minimal valid CLI-mode payload."""
        base: dict = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "session_summary": {"headline": "ok", "key_findings": [], "body": ""},
            "dispatched_via": "cli",
            "cli_agent": "claude_code",
        }
        base.update(overrides)
        return base

    def test_valid_sdk_mode_constructs(self) -> None:
        """Happy path: valid SDK-mode AnalysisOutput constructs without error."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(self._sdk_payload())
        assert output.schema_version == "1.0"
        assert output.dispatched_via == "sdk"
        assert output.primary_model == "claude-opus-4-5"
        assert output.cli_agent is None
        assert output.fallback_used is False
        assert output.retry_count == 0
        assert output.status == "success"

    def test_valid_cli_mode_constructs(self) -> None:
        """Happy path: valid CLI-mode AnalysisOutput constructs without error."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(self._cli_payload())
        assert output.dispatched_via == "cli"
        assert output.cli_agent == "claude_code"
        assert output.primary_model is None
        assert output.fallback_used is False

    def test_behavior_flags_empty_is_valid(self) -> None:
        """DC3: empty behavior_flags list is a valid shape.
        model refused or saw nothing; downstream warns on >N events but flags=[].
        The shape itself must not reject this."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(self._sdk_payload(behavior_flags=[]))
        assert output.behavior_flags == []

    def test_unknown_status_valid_with_all_fields(self) -> None:
        """status='unknown' still requires the same field invariants as 'success'.
        Decision: Option A — dispatched_via tells us which mode was attempted.
        Forensically valuable even in unknown state."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(self._sdk_payload(status="unknown"))
        assert output.status == "unknown"
        assert output.primary_model == "claude-opus-4-5"
        assert output.dispatched_via == "sdk"

    def test_failure_status_valid_with_error_details(self) -> None:
        """status='failure' with error_details dict carries both provider errors (DC4)."""
        from secondsight.analysis.output import AnalysisOutput

        error_details = {
            "primary_error": "AnthropicError: 529 Overloaded",
            "fallback_error": "OpenAIError: 401 Unauthorized",
        }
        output = AnalysisOutput.model_validate(
            self._sdk_payload(
                status="failure",
                error_details=error_details,
                fallback_used=True,
            )
        )
        assert output.status == "failure"
        assert output.error_details is not None
        assert "primary_error" in output.error_details
        assert "fallback_error" in output.error_details
        assert output.fallback_used is True

    def test_retry_count_boundary_valid(self) -> None:
        """retry_count=0, 1, 2, 5 are all valid within the Phase 1 hard cap."""
        from secondsight.analysis.output import AnalysisOutput

        for count in (0, 1, 2, 5):
            output = AnalysisOutput.model_validate(self._sdk_payload(retry_count=count))
            assert output.retry_count == count

    def test_round_trip_serialization(self) -> None:
        """JSON serialization + parse back produces identical instance.
        Frozen=True means equality is field-value equality."""
        from secondsight.analysis.output import AnalysisOutput

        original = AnalysisOutput.model_validate(self._sdk_payload())
        json_str = original.model_dump_json()
        parsed = AnalysisOutput.model_validate_json(json_str)
        assert original == parsed

    def test_round_trip_with_behavior_flags(self) -> None:
        """Round-trip with non-empty behavior_flags preserves flag fields.
        Uses BehaviorFlagDraft (LLM-emittable shape) — no DB identity fields."""
        from secondsight.analysis.output import AnalysisOutput
        from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType

        flag = BehaviorFlagDraft(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["evt-1", "evt-2"],
            reason="Agent read README before fixing bug",
            confidence="high",
        )
        payload = self._sdk_payload(behavior_flags=[flag.model_dump()])
        original = AnalysisOutput.model_validate(payload)
        json_str = original.model_dump_json()
        parsed = AnalysisOutput.model_validate_json(json_str)
        assert original == parsed
        assert len(parsed.behavior_flags) == 1
        assert parsed.behavior_flags[0].flag_type == BehaviorFlagType.UNNECESSARY_READ

    def test_unknown_status_with_behavior_flags_is_valid(self) -> None:
        """Decision A: unknown status allows populated behavior_flags as best-effort partial data.
        The dispatcher may have partially completed analysis before hitting unknown state."""
        from secondsight.analysis.output import AnalysisOutput

        # unknown status + empty flags = valid
        output = AnalysisOutput.model_validate(self._sdk_payload(status="unknown"))
        assert output.status == "unknown"
        assert output.behavior_flags == []

    def test_error_details_none_by_default(self) -> None:
        """error_details defaults to None for success outcomes."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(self._sdk_payload())
        assert output.error_details is None

    def test_cli_retry_count_set(self) -> None:
        """CLI mode may report retry_count up to the Phase 1 hard cap."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(
            self._cli_payload(
                status="failure",
                retry_count=5,
                error_details={"reason": "schema mismatch after 5 retries"},
            )
        )
        assert output.retry_count == 5
        assert output.status == "failure"

    def test_json_schema_is_serializable(self) -> None:
        """model_json_schema() returns a dict that can be JSON-serialized.
        This will be embedded in jinja prompts (Task 3)."""
        from secondsight.analysis.output import AnalysisOutput

        schema = AnalysisOutput.model_json_schema()
        assert isinstance(schema, dict)
        # Must be JSON-serializable without error
        json_str = json.dumps(schema)
        assert len(json_str) > 0
        # Must have key fields in schema
        assert "properties" in schema or "$defs" in schema

    def test_frozen_model_rejects_mutation(self) -> None:
        """frozen=True: attempting to set a field after construction raises TypeError."""
        from secondsight.analysis.output import AnalysisOutput

        output = AnalysisOutput.model_validate(self._sdk_payload())
        with pytest.raises((TypeError, ValidationError)):
            output.status = "failure"  # type: ignore[misc]


class TestSessionSummaryConstruction:
    """Verify SessionSummary sub-model behaves correctly."""

    def test_valid_session_summary(self) -> None:
        """SessionSummary with all fields constructs successfully."""
        from secondsight.analysis.output import SessionSummary

        summary = SessionSummary(
            headline="Agent over-read files",
            key_findings=["Finding A", "Finding B"],
            body="Detailed analysis body here.",
        )
        assert summary.headline == "Agent over-read files"
        assert len(summary.key_findings) == 2

    def test_session_summary_extra_field_raises(self) -> None:
        """SessionSummary also enforces extra='forbid'."""
        from secondsight.analysis.output import SessionSummary

        with pytest.raises(ValidationError):
            SessionSummary.model_validate(
                {
                    "headline": "ok",
                    "key_findings": [],
                    "body": "",
                    "injected": "bad",
                }
            )


class TestBehaviorFlagDraftConstruction:
    """Verify BehaviorFlagDraft sub-model (LLM-emittable shape) behaves correctly.

    BehaviorFlagDraft carries only the fields the LLM can produce:
    flag_type, event_ids, reason, confidence. No DB identity fields.
    The orchestrator promotes Draft → BehaviorFlag by injecting persistence fields.
    """

    def test_valid_behavior_flag_draft(self) -> None:
        """BehaviorFlagDraft constructs with the LLM-emittable fields only."""
        from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType

        flag = BehaviorFlagDraft(
            flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
            event_ids=["e1"],
            reason="reason text",
            confidence="medium",
        )
        assert flag.confidence == "medium"
        assert flag.flag_type == BehaviorFlagType.REDUNDANT_EXPLORATION

    def test_behavior_flag_draft_rejects_db_identity_fields(self) -> None:
        """BehaviorFlagDraft enforces extra='forbid'.
        DB identity fields (id, project_id, session_id, segment_index, created_at)
        that the LLM cannot produce MUST be rejected — they belong only in BehaviorFlag."""
        from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType

        with pytest.raises(ValidationError):
            BehaviorFlagDraft.model_validate(
                {
                    "flag_type": BehaviorFlagType.UNNECESSARY_READ.value,
                    "event_ids": ["e1"],
                    "reason": "reason",
                    "confidence": "high",
                    "id": "abc123",  # DB identity field — LLM cannot produce this
                }
            )

    def test_behavior_flag_draft_extra_field_raises(self) -> None:
        """BehaviorFlagDraft enforces extra='forbid' for any unknown field."""
        from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType

        with pytest.raises(ValidationError):
            BehaviorFlagDraft.model_validate(
                {
                    "flag_type": BehaviorFlagType.UNNECESSARY_READ.value,
                    "event_ids": ["e1"],
                    "reason": "reason",
                    "confidence": "high",
                    "injected_meta": "bad",
                }
            )
