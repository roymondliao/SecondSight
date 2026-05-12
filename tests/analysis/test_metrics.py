"""Death + happy-path tests for compute_segment_metrics (GUR-100 task-5)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from secondsight.analysis.metrics import compute_segment_metrics
from secondsight.analysis.schemas import SegmentData, ToolUseSpan


_BASE_TS = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ts(offset_seconds: float) -> str:
    return (_BASE_TS + timedelta(seconds=offset_seconds)).isoformat()


def _segment(events: list) -> SegmentData:
    return SegmentData(
        segment_index=1,
        user_prompt={"id": "up-1", "data": {"text": "go"}},
        events=events,
        session_id="sess-1",
        project_id="proj-1",
    )


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_5_1_null_token_count_logs_warning_and_contributes_zero(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DT-5.1 — null token_count contributes 0 with WARNING; never raises."""
        events = [
            {
                "id": "evt-think-null",
                "event_type": "thinking",
                "sequence_number": 1,
                "timestamp": _ts(0),
                "token_count": None,
            },
            {
                "id": "evt-think-500",
                "event_type": "thinking",
                "sequence_number": 2,
                "timestamp": _ts(1),
                "token_count": 500,
            },
        ]
        with caplog.at_level(logging.WARNING, logger="secondsight.analysis.metrics"):
            metrics = compute_segment_metrics(_segment(events))

        assert metrics["total_tokens"] == 500
        assert "evt-think-null" in caplog.text
        assert "null token_count" in caplog.text.lower()

    def test_dt_5_2_single_event_segment_duration_is_zero_not_none(
        self,
    ) -> None:
        """DT-5.2 — one-event segment has duration=0.0 (not None)."""
        events = [
            {
                "id": "u",
                "event_type": "thinking",
                "sequence_number": 1,
                "timestamp": _ts(0),
                "token_count": 100,
            },
        ]
        m = compute_segment_metrics(_segment(events))
        assert m["duration"] == 0.0
        assert isinstance(m["duration"], float)

    def test_dt_5_3_empty_segment_returns_all_zero_metrics(self) -> None:
        """DT-5.3 — events=[] returns the all-zero baseline; never raises."""
        m = compute_segment_metrics(_segment([]))
        assert m == {
            "total_tokens": 0,
            "unique_files": 0,
            "duration": 0.0,
            "error_count": 0,
        }

    def test_dt_5_4_orphan_span_success_none_does_not_count_as_error(
        self,
    ) -> None:
        """DT-5.4 — ToolUseSpan(success=None) is unknown, not failed."""
        span_unknown = ToolUseSpan(
            tool_name="Read",
            target="/x.py",
            success=None,
            duration_ms=None,
            start_seq=1,
            end_seq=None,
        )
        m = compute_segment_metrics(_segment([span_unknown]))
        assert m["error_count"] == 0


# =====================================================================
# HAPPY PATHS
# =====================================================================


class TestHappyPaths:
    def test_five_event_fixture_matches_hand_computed(self) -> None:
        """5-event fixture: 2 thinking (1000+1500 tokens), 3 tool spans
        (Read /a.py success 200ms, Edit /a.py success 100ms, Bash 'pytest'
        failure 2100ms), spanning 7.5 seconds.
        Hand-computed: total_tokens=2500, unique_files=1 (only /a.py
        since Bash is not file-touching), duration=7.5, error_count=1.
        """
        thinking_1 = {
            "id": "t-1",
            "event_type": "thinking",
            "sequence_number": 1,
            "timestamp": _ts(0),
            "token_count": 1000,
        }
        thinking_2 = {
            "id": "t-2",
            "event_type": "thinking",
            "sequence_number": 4,
            "timestamp": _ts(2.5),
            "token_count": 1500,
        }
        read_span = ToolUseSpan(
            tool_name="Read",
            target="/a.py",
            success=True,
            duration_ms=200,
            start_seq=2,
            end_seq=3,
        )
        edit_span = ToolUseSpan(
            tool_name="Edit",
            target="/a.py",
            success=True,
            duration_ms=100,
            start_seq=5,
            end_seq=6,
        )
        bash_span = ToolUseSpan(
            tool_name="Bash",
            target="pytest",
            success=False,
            duration_ms=2100,
            start_seq=7,
            end_seq=8,
        )
        # Span events emit no own timestamps in our model; the
        # surrounding raw events anchor the segment timeline.
        end_anchor = {
            "id": "anchor",
            "event_type": "response",
            "sequence_number": 9,
            "timestamp": _ts(7.5),
            "token_count": 0,
        }
        events = [
            thinking_1,
            read_span,
            thinking_2,
            edit_span,
            bash_span,
            end_anchor,
        ]
        m = compute_segment_metrics(_segment(events))
        assert m["total_tokens"] == 2500
        assert m["unique_files"] == 1
        assert m["duration"] == 7.5
        assert m["error_count"] == 1

    def test_purity_idempotent_on_same_input(self) -> None:
        events = [
            {
                "id": "t",
                "event_type": "thinking",
                "sequence_number": 1,
                "timestamp": _ts(0),
                "token_count": 100,
            },
            ToolUseSpan(
                tool_name="Read",
                target="/x.py",
                success=True,
                duration_ms=50,
                start_seq=2,
                end_seq=3,
            ),
        ]
        seg = _segment(events)
        m1 = compute_segment_metrics(seg)
        m2 = compute_segment_metrics(seg)
        assert m1 == m2

    def test_unique_files_dedupes_across_file_touching_tools(self) -> None:
        events = [
            ToolUseSpan(
                tool_name="Read",
                target="/a.py",
                success=True,
                duration_ms=10,
                start_seq=1,
                end_seq=2,
            ),
            ToolUseSpan(
                tool_name="Edit",
                target="/a.py",
                success=True,
                duration_ms=10,
                start_seq=3,
                end_seq=4,
            ),
            ToolUseSpan(
                tool_name="Read",
                target="/b.py",
                success=True,
                duration_ms=10,
                start_seq=5,
                end_seq=6,
            ),
        ]
        m = compute_segment_metrics(_segment(events))
        assert m["unique_files"] == 2

    def test_non_file_touching_tool_excluded_from_unique_files(self) -> None:
        """Bash is not in FILE_TOUCHING_TOOLS, so its target should
        not bump unique_files — even though it has a target string."""
        events = [
            ToolUseSpan(
                tool_name="Bash",
                target="pytest tests/",
                success=True,
                duration_ms=100,
                start_seq=1,
                end_seq=2,
            ),
        ]
        m = compute_segment_metrics(_segment(events))
        assert m["unique_files"] == 0

    def test_session_start_with_null_token_count_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """session_start carries no token_count by design; should NOT
        emit a WARNING. Only thinking/response with null tokens warn.
        """
        events = [
            {
                "id": "ss",
                "event_type": "session_start",
                "sequence_number": 1,
                "timestamp": _ts(0),
                "token_count": None,
            },
        ]
        with caplog.at_level(logging.WARNING, logger="secondsight.analysis.metrics"):
            compute_segment_metrics(_segment(events))
        assert "null token_count" not in caplog.text.lower()
