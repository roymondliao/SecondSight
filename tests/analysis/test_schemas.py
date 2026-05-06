"""Death + coverage tests for secondsight.analysis.schemas (GUR-100 task-1).

Death-first ordering: tests that catch silent failures appear before
happy-path tests. Each death test names the failure mode it closes.

Coverage anchors: SD §5.5.1 (BehaviorFlagType vocabulary, six values),
SD §7.4 (DirectiveStatus vocabulary, five values; DirectiveType, two
values), and the D3 contract additions (BehaviorFlag.confidence,
Directive.disabled_at / disabled_reason).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
    SegmentData,
    ToolUseSpan,
)


# ---------- helpers ----------


def _now() -> datetime:
    return datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _flag_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "flag-1",
        "project_id": "proj-1",
        "session_id": "sess-1",
        "segment_index": 1,
        "flag_type": BehaviorFlagType.UNNECESSARY_READ,
        "event_ids": ["e1", "e2"],
        "intent_summary": "fix bug in utils.py",
        "reason": "config.yaml unrelated to bug fix",
        "confidence": "high",
        "created_at": _now(),
    }
    base.update(overrides)
    return base


def _directive_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "dir-1",
        "project_id": "proj-1",
        "type": DirectiveType.CONVENTION,
        "status": DirectiveStatus.ACTIVE,
        "instruction": "Skip exploration when path is given",
        "frequency": 0.7,
        "source_flag_type": "unnecessary_read",
        "source_sessions": ["s1", "s2"],
        "created_at": _now(),
        "updated_at": _now(),
    }
    base.update(overrides)
    return base


# =====================================================================
# DEATH TESTS — silent-failure paths closed by Pydantic validators
# =====================================================================


class TestDeathPaths:
    def test_dt_1_1_behavior_flag_rejects_unknown_flag_type(self) -> None:
        """DT-1.1 — invalid flag_type is rejected at construction."""
        with pytest.raises(ValidationError) as exc:
            BehaviorFlag(**_flag_kwargs(flag_type="bogus_type"))
        assert "flag_type" in str(exc.value)

    def test_dt_1_2_behavior_flag_rejects_unknown_confidence(self) -> None:
        """DT-1.2 — confidence outside {high,medium,low} is rejected."""
        with pytest.raises(ValidationError) as exc:
            BehaviorFlag(**_flag_kwargs(confidence="kinda"))
        assert "confidence" in str(exc.value)

    def test_dt_1_3_directive_rejects_unknown_status(self) -> None:
        """DT-1.3 — invalid DirectiveStatus is rejected."""
        with pytest.raises(ValidationError) as exc:
            Directive(**_directive_kwargs(status="frozen"))
        assert "status" in str(exc.value)

    def test_dt_1_4_tool_use_span_rejects_success_with_null_duration(
        self,
    ) -> None:
        """DT-1.4 — success=True with duration_ms=None is incoherent.

        A successful span MUST carry a measured duration. None duration
        is the orphan-start signal (success=None). Conflating them
        would let a downstream consumer treat an unknown-outcome span
        as a measured success.
        """
        with pytest.raises(ValidationError) as exc:
            ToolUseSpan(
                tool_name="Read",
                target="/x.py",
                success=True,
                duration_ms=None,
                start_seq=1,
                end_seq=2,
            )
        msg = str(exc.value).lower()
        assert "duration" in msg

    def test_dt_1_5_behavior_flag_type_has_exactly_six_sd_551_values(
        self,
    ) -> None:
        """DT-1.5 — SD §5.5.1 vocabulary is exactly these six strings.

        Adding/removing a value is a coordinated SD-update + downstream
        prompt rewrite. This test is the canary.
        """
        expected = {
            "unnecessary_read",
            "redundant_exploration",
            "missed_shortcut",
            "repeated_operation",
            "wrong_tool_choice",
            "excessive_context_gathering",
        }
        actual = {member.value for member in BehaviorFlagType}
        assert actual == expected, (
            f"BehaviorFlagType drifted from SD §5.5.1. "
            f"missing={expected - actual}, extra={actual - expected}"
        )

    def test_dt_1_6_directive_status_has_exactly_five_sd_74_values(
        self,
    ) -> None:
        """DT-1.6 — SD §7.4 status vocabulary is exactly these five strings."""
        expected = {"active", "disabled", "expired", "superseded", "obsolete"}
        actual = {m.value for m in DirectiveStatus}
        assert actual == expected, (
            f"DirectiveStatus drifted from SD §7.4. "
            f"missing={expected - actual}, extra={actual - expected}"
        )

    def test_dt_1_7_segment_data_accepts_pre_prompt_empty_segment(self) -> None:
        """DT-1.7 — pre-prompt segment (user_prompt=None, events=[]) is valid.

        The implicit segment_index=0 with no triggering USER_PROMPT must
        round-trip; the segmenter relies on this shape.
        """
        seg = SegmentData(
            segment_index=0,
            user_prompt=None,
            events=[],
            session_id="sess-1",
            project_id="proj-1",
        )
        assert seg.user_prompt is None
        assert seg.events == []


# =====================================================================
# ENUM COVERAGE — every value present, exact string match
# =====================================================================


class TestEnumCoverage:
    @pytest.mark.parametrize(
        ("member", "expected_value"),
        [
            (BehaviorFlagType.UNNECESSARY_READ, "unnecessary_read"),
            (BehaviorFlagType.REDUNDANT_EXPLORATION, "redundant_exploration"),
            (BehaviorFlagType.MISSED_SHORTCUT, "missed_shortcut"),
            (BehaviorFlagType.REPEATED_OPERATION, "repeated_operation"),
            (BehaviorFlagType.WRONG_TOOL_CHOICE, "wrong_tool_choice"),
            (
                BehaviorFlagType.EXCESSIVE_CONTEXT_GATHERING,
                "excessive_context_gathering",
            ),
        ],
    )
    def test_behavior_flag_type_value_exact(
        self, member: BehaviorFlagType, expected_value: str
    ) -> None:
        assert member.value == expected_value
        assert BehaviorFlagType(expected_value) is member

    @pytest.mark.parametrize(
        ("member", "expected_value"),
        [
            (DirectiveStatus.ACTIVE, "active"),
            (DirectiveStatus.DISABLED, "disabled"),
            (DirectiveStatus.EXPIRED, "expired"),
            (DirectiveStatus.SUPERSEDED, "superseded"),
            (DirectiveStatus.OBSOLETE, "obsolete"),
        ],
    )
    def test_directive_status_value_exact(
        self, member: DirectiveStatus, expected_value: str
    ) -> None:
        assert member.value == expected_value
        assert DirectiveStatus(expected_value) is member

    @pytest.mark.parametrize(
        ("member", "expected_value"),
        [
            (DirectiveType.CONVENTION, "convention"),
            (DirectiveType.HINT, "hint"),
        ],
    )
    def test_directive_type_value_exact(
        self, member: DirectiveType, expected_value: str
    ) -> None:
        assert member.value == expected_value
        assert DirectiveType(expected_value) is member


# =====================================================================
# ROUND-TRIP — Pydantic dump/load preserves all fields
# =====================================================================


class TestRoundTrip:
    def test_behavior_flag_round_trips(self) -> None:
        original = BehaviorFlag(**_flag_kwargs())
        dumped = original.model_dump(mode="json")
        rebuilt = BehaviorFlag.model_validate(dumped)
        assert rebuilt == original
        # event_ids preserved as ordered list
        assert rebuilt.event_ids == ["e1", "e2"]

    def test_directive_round_trips_with_optional_fields_none(self) -> None:
        d = Directive(**_directive_kwargs())
        dumped = d.model_dump(mode="json")
        rebuilt = Directive.model_validate(dumped)
        assert rebuilt == d
        assert rebuilt.disabled_at is None
        assert rebuilt.disabled_reason is None
        assert rebuilt.expires_at is None

    def test_directive_round_trips_with_disable_metadata(self) -> None:
        d = Directive(
            **_directive_kwargs(
                status=DirectiveStatus.DISABLED,
                disabled_at=_now(),
                disabled_reason="superseded by D-2",
            )
        )
        dumped = d.model_dump(mode="json")
        rebuilt = Directive.model_validate(dumped)
        assert rebuilt == d
        assert rebuilt.disabled_at is not None
        assert rebuilt.disabled_reason == "superseded by D-2"

    def test_tool_use_span_orphan_start_is_valid(self) -> None:
        span = ToolUseSpan(
            tool_name="Read",
            target="/x.py",
            success=None,
            duration_ms=None,
            start_seq=10,
            end_seq=None,
        )
        assert span.success is None
        assert span.end_seq is None

    def test_tool_use_span_paired_round_trips(self) -> None:
        span = ToolUseSpan(
            tool_name="Edit",
            target="/x.py",
            success=True,
            duration_ms=120,
            start_seq=4,
            end_seq=5,
            metadata={"line_range": "42-45"},
        )
        rebuilt = ToolUseSpan.model_validate(span.model_dump())
        assert rebuilt == span

    def test_tool_use_span_failure_with_duration_is_valid(self) -> None:
        """success=False with measured duration is a normal failed tool call."""
        span = ToolUseSpan(
            tool_name="Bash",
            target="pytest",
            success=False,
            duration_ms=2100,
            start_seq=8,
            end_seq=9,
        )
        assert span.success is False

    def test_segment_data_with_heterogeneous_events(self) -> None:
        thinking_event = {
            "id": "evt-think",
            "event_type": "thinking",
            "sequence_number": 3,
            "token_count": 1500,
        }
        span = ToolUseSpan(
            tool_name="Read",
            target="/x.py",
            success=True,
            duration_ms=80,
            start_seq=4,
            end_seq=5,
        )
        seg = SegmentData(
            segment_index=1,
            user_prompt={"id": "evt-up", "data": {"text": "fix bug"}},
            events=[thinking_event, span],
            session_id="sess-1",
            project_id="proj-1",
        )
        assert len(seg.events) == 2
        # Thinking event passed through as dict.
        assert isinstance(seg.events[0], dict)
        # Tool-use span preserved as ToolUseSpan instance.
        assert isinstance(seg.events[1], ToolUseSpan)
