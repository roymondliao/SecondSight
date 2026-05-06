"""Death + happy-path tests for Segmenter (GUR-100 task-4).

Uses an in-memory stub EventsRepository to feed synthetic event streams
through the segmenter without a real DB. The segmenter only depends on
EventsRepository.get_session_events; the stub satisfies that contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from secondsight.analysis.schemas import SegmentData, ToolUseSpan
from secondsight.analysis.segmenter import Segmenter
from secondsight.event import Event, EventType


_BASE_TS = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _evt(
    *,
    seq: int,
    event_type: EventType,
    segment_index: int = 0,
    data: dict | None = None,
    duration_ms: int | None = None,
    token_count: int | None = None,
    sub_agent_id: str | None = None,
    depth: int = 0,
    session_id: str = "sess-1",
    project_id: str = "proj-1",
    id_prefix: str = "evt",
) -> Event:
    return Event(
        id=f"{id_prefix}-{seq}",
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=_BASE_TS + timedelta(seconds=seq),
        sequence_number=seq,
        segment_index=segment_index,
        sub_agent_id=sub_agent_id,
        depth=depth,
        duration_ms=duration_ms,
        token_count=token_count,
        data=data or {},
    )


class _StubEventsRepo:
    def __init__(self, events: list[Event]) -> None:
        self._events = events

    def get_session_events(self, session_id: str) -> list[Event]:
        return [e for e in self._events if e.session_id == session_id]


# =====================================================================
# DEATH TESTS — ordered before happy paths
# =====================================================================


class TestDeathPaths:
    def test_dt_4_1_orphan_tool_use_start_emits_span_with_success_none(
        self,
    ) -> None:
        """Server crashed mid-tool: start at seq=5, no matching end.
        Span MUST be emitted (success=None, end_seq=None).
        """
        events = [
            _evt(seq=1, event_type=EventType.SESSION_START, segment_index=0),
            _evt(
                seq=2,
                event_type=EventType.USER_PROMPT,
                segment_index=1,
                data={"text": "do thing"},
            ),
            _evt(
                seq=5,
                event_type=EventType.TOOL_USE_START,
                segment_index=1,
                data={"tool_name": "Read", "target": "/x.py"},
            ),
        ]
        seg = Segmenter(_StubEventsRepo(events))
        out = seg.segment_session("sess-1")
        # segments: 0 (session_start), 1 (orphan span)
        seg_idx_1 = next(s for s in out if s.segment_index == 1)
        spans = [e for e in seg_idx_1.events if isinstance(e, ToolUseSpan)]
        assert len(spans) == 1, "orphan tool_use_start was silently dropped"
        assert spans[0].start_seq == 5
        assert spans[0].end_seq is None
        assert spans[0].success is None
        assert spans[0].duration_ms is None

    def test_dt_4_2_orphan_tool_use_end_emits_synthesized_span_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Orphan tool_use_end (no preceding start) is synthesized as
        a 0-width span with WARNING log.
        """
        events = [
            _evt(seq=1, event_type=EventType.USER_PROMPT, segment_index=1),
            _evt(
                seq=7,
                event_type=EventType.TOOL_USE_END,
                segment_index=1,
                data={
                    "tool_name": "Read",
                    "target": "/x.py",
                    "success": True,
                },
                duration_ms=120,
            ),
        ]
        with caplog.at_level(
            logging.WARNING, logger="secondsight.analysis.segmenter"
        ):
            seg = Segmenter(_StubEventsRepo(events))
            out = seg.segment_session("sess-1")

        seg_idx_1 = next(s for s in out if s.segment_index == 1)
        spans = [e for e in seg_idx_1.events if isinstance(e, ToolUseSpan)]
        assert len(spans) == 1
        assert spans[0].start_seq == 7
        assert spans[0].end_seq == 7
        # The success-with-duration_ms case is tolerated for end-only
        # synthesis; we trust the end event's recorded outcome.
        assert "orphan_tool_use_end" in caplog.text

    def test_dt_4_3_pre_prompt_segment_is_emitted_separately(self) -> None:
        """Events with segment_index=0 (no preceding USER_PROMPT) must
        be emitted as their own SegmentData with user_prompt=None.
        """
        events = [
            _evt(seq=1, event_type=EventType.SESSION_START, segment_index=0),
            _evt(seq=2, event_type=EventType.USER_PROMPT, segment_index=1),
        ]
        seg = Segmenter(_StubEventsRepo(events))
        out = seg.segment_session("sess-1")
        idxs = [s.segment_index for s in out]
        assert 0 in idxs
        assert 1 in idxs
        s0 = next(s for s in out if s.segment_index == 0)
        assert s0.user_prompt is None
        assert len(s0.events) == 1, "pre-prompt event silently elided"

    def test_dt_4_4_empty_segment_is_emitted_with_empty_events(self) -> None:
        """USER_PROMPT followed immediately by next USER_PROMPT yields
        an empty segment for the first prompt. Must NOT be dropped.
        """
        events = [
            _evt(seq=1, event_type=EventType.USER_PROMPT, segment_index=1),
            _evt(seq=2, event_type=EventType.USER_PROMPT, segment_index=2),
            _evt(
                seq=3,
                event_type=EventType.TOOL_USE_START,
                segment_index=2,
                data={"tool_name": "Read", "target": "/x.py"},
            ),
            _evt(
                seq=4,
                event_type=EventType.TOOL_USE_END,
                segment_index=2,
                data={
                    "tool_name": "Read",
                    "target": "/x.py",
                    "success": True,
                },
                duration_ms=100,
            ),
        ]
        seg = Segmenter(_StubEventsRepo(events))
        out = seg.segment_session("sess-1")
        idxs = [s.segment_index for s in out]
        assert 1 in idxs
        assert 2 in idxs
        s1 = next(s for s in out if s.segment_index == 1)
        # empty segment 1: user_prompt set, events=[]
        assert s1.user_prompt is not None
        assert s1.events == [], "empty segment was silently elided"

    def test_dt_4_5_out_of_order_sequence_number_raises(self) -> None:
        """Out-of-order seq numbers must surface, never silently re-sort."""
        # Build events with internal duplicate sequence_number.
        events = [
            _evt(seq=1, event_type=EventType.USER_PROMPT, segment_index=1),
            _evt(seq=3, event_type=EventType.THINKING, segment_index=1),
            # Same sequence_number 3 — boundary violation when iterated.
            _evt(seq=3, event_type=EventType.THINKING, segment_index=1, id_prefix="evt-dup"),
        ]
        # Stub returns events in the order given (out of strict
        # monotonic increase). Real repo orders by sequence_number;
        # this test injects what the segmenter sees post-order.
        seg = Segmenter(_StubEventsRepo(events))
        with pytest.raises(ValueError) as exc:
            seg.segment_session("sess-1")
        assert "sequence_number" in str(exc.value)


# =====================================================================
# HAPPY PATHS
# =====================================================================


class TestHappyPaths:
    def test_paired_tool_use_assembles_into_span(self) -> None:
        events = [
            _evt(seq=1, event_type=EventType.USER_PROMPT, segment_index=1),
            _evt(
                seq=4,
                event_type=EventType.TOOL_USE_START,
                segment_index=1,
                data={"tool_name": "Read", "target": "/x.py"},
            ),
            _evt(
                seq=5,
                event_type=EventType.TOOL_USE_END,
                segment_index=1,
                data={
                    "tool_name": "Read",
                    "target": "/x.py",
                    "success": True,
                },
                duration_ms=120,
            ),
        ]
        seg = Segmenter(_StubEventsRepo(events))
        out = seg.segment_session("sess-1")
        s1 = next(s for s in out if s.segment_index == 1)
        spans = [e for e in s1.events if isinstance(e, ToolUseSpan)]
        assert len(spans) == 1
        span = spans[0]
        assert span.tool_name == "Read"
        assert span.target == "/x.py"
        assert span.success is True
        assert span.duration_ms == 120
        assert span.start_seq == 4
        assert span.end_seq == 5

    def test_full_session_three_segments(self) -> None:
        """8-event session → 3 SegmentData with correct event placement."""
        events = [
            _evt(
                seq=0, event_type=EventType.SESSION_START, segment_index=0
            ),
            _evt(seq=1, event_type=EventType.USER_PROMPT, segment_index=1),
            _evt(
                seq=2,
                event_type=EventType.THINKING,
                segment_index=1,
                token_count=1000,
            ),
            _evt(
                seq=3,
                event_type=EventType.TOOL_USE_START,
                segment_index=1,
                data={"tool_name": "Read", "target": "/a.py"},
            ),
            _evt(
                seq=4,
                event_type=EventType.TOOL_USE_END,
                segment_index=1,
                data={
                    "tool_name": "Read",
                    "target": "/a.py",
                    "success": True,
                },
                duration_ms=80,
            ),
            _evt(seq=5, event_type=EventType.USER_PROMPT, segment_index=2),
            _evt(
                seq=6,
                event_type=EventType.TOOL_USE_START,
                segment_index=2,
                data={"tool_name": "Edit", "target": "/a.py"},
            ),
            _evt(
                seq=7,
                event_type=EventType.TOOL_USE_END,
                segment_index=2,
                data={
                    "tool_name": "Edit",
                    "target": "/a.py",
                    "success": True,
                },
                duration_ms=200,
            ),
        ]
        seg = Segmenter(_StubEventsRepo(events))
        out = seg.segment_session("sess-1")
        idxs = sorted(s.segment_index for s in out)
        assert idxs == [0, 1, 2]

        s1 = next(s for s in out if s.segment_index == 1)
        s2 = next(s for s in out if s.segment_index == 2)
        # segment 1: thinking event + paired ToolUseSpan
        s1_spans = [e for e in s1.events if isinstance(e, ToolUseSpan)]
        s1_dicts = [e for e in s1.events if isinstance(e, dict)]
        assert len(s1_spans) == 1
        assert len(s1_dicts) == 1  # the thinking event
        assert s1_dicts[0]["event_type"] == "thinking"
        # segment 2: just the paired Edit span
        s2_spans = [e for e in s2.events if isinstance(e, ToolUseSpan)]
        assert len(s2_spans) == 1
        assert s2_spans[0].tool_name == "Edit"

    def test_sub_agent_events_pass_through_as_dicts(self) -> None:
        events = [
            _evt(seq=1, event_type=EventType.USER_PROMPT, segment_index=1),
            _evt(
                seq=2,
                event_type=EventType.SUB_AGENT_START,
                segment_index=1,
                data={"sub_agent_id": "child-1"},
                sub_agent_id="child-1",
                depth=1,
            ),
            _evt(
                seq=3,
                event_type=EventType.TOOL_USE_START,
                segment_index=1,
                data={"tool_name": "Glob", "target": "*.py"},
                sub_agent_id="child-1",
                depth=1,
            ),
            _evt(
                seq=4,
                event_type=EventType.TOOL_USE_END,
                segment_index=1,
                data={
                    "tool_name": "Glob",
                    "target": "*.py",
                    "success": True,
                },
                duration_ms=60,
                sub_agent_id="child-1",
                depth=1,
            ),
            _evt(
                seq=5,
                event_type=EventType.SUB_AGENT_END,
                segment_index=1,
                data={"sub_agent_id": "child-1"},
            ),
        ]
        seg = Segmenter(_StubEventsRepo(events))
        out = seg.segment_session("sess-1")
        s1 = next(s for s in out if s.segment_index == 1)
        # Expect: sub_agent_start dict, ToolUseSpan, sub_agent_end dict.
        types = []
        for e in s1.events:
            if isinstance(e, ToolUseSpan):
                types.append("span")
            else:
                types.append(e["event_type"])
        assert types == ["sub_agent_start", "span", "sub_agent_end"]

    def test_no_events_returns_empty_list(self) -> None:
        seg = Segmenter(_StubEventsRepo([]))
        assert seg.segment_session("sess-1") == []
