"""Segmenter — read-side event assembler (GUR-100 task-4).

CRITICAL: this is an ASSEMBLER, not a re-segmenter. Events arrive
pre-segmented by SessionTracker.bind() (the writer side; see
src/secondsight/observation/tracker.py — segment_index increments only
on USER_PROMPT events). The segmenter:
  1. Loads all events for a session.
  2. Groups by segment_index.
  3. Pairs tool_use_start/end into ToolUseSpan; other event types pass
     through as raw dicts.
  4. Returns list[SegmentData].

Death cases enforced here (NEVER silently):
- Orphan tool_use_start (no matching end) → ToolUseSpan(success=None,
  end_seq=None). Span is emitted, never omitted.
- Orphan tool_use_end (no preceding start) → synthesized ToolUseSpan
  with start_seq == end_seq, plus a WARNING log naming
  "orphan_tool_use_end".
- Out-of-order sequence_number → raise ValueError. NEVER silently re-sort.
- Pre-prompt segment (segment_index=0 with no USER_PROMPT) → emitted
  with user_prompt=None.
- Empty segment (USER_PROMPT followed immediately by next USER_PROMPT)
  → emitted with events=[].

Pairing strategy: tool_use_start is queued by (tool_name, target).
The next tool_use_end with the same key consumes the oldest start in
the queue. This handles correctly-nested concurrent tools of different
kinds; identical (tool_name, target) calls in flight share a FIFO queue.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from secondsight.analysis.schemas import SegmentData, ToolUseSpan
from secondsight.event import Event, EventType
from secondsight.storage.events_repository import EventsRepository

_logger = logging.getLogger(__name__)


class Segmenter:
    """Read-side assembler from events table to SegmentData list."""

    def __init__(self, events_repo: EventsRepository) -> None:
        self._events_repo = events_repo

    def segment_session(self, session_id: str) -> list[SegmentData]:
        events = self._events_repo.get_session_events(session_id)
        if not events:
            return []

        self._validate_sequence_order(events)

        # Group by segment_index, preserving in-segment order
        # (events from get_session_events are already sorted by
        # sequence_number).
        by_segment: dict[int, list[Event]] = defaultdict(list)
        for event in events:
            by_segment[event.segment_index].append(event)

        # Project_id is session-uniform; take it from the first event.
        project_id = events[0].project_id

        segments: list[SegmentData] = []
        for segment_index in sorted(by_segment):
            seg_events = by_segment[segment_index]
            user_prompt = self._extract_user_prompt(seg_events)
            assembled = self._assemble_events(
                seg_events, exclude_user_prompt=True
            )
            segments.append(
                SegmentData(
                    segment_index=segment_index,
                    user_prompt=user_prompt,
                    events=assembled,
                    session_id=session_id,
                    project_id=project_id,
                )
            )
        return segments

    @staticmethod
    def _validate_sequence_order(events: list[Event]) -> None:
        """Sequence numbers must be strictly monotonically increasing.

        SessionTracker assigns these on ingest; out-of-order means the
        DB has been corrupted (manual edit, partial write, or a real
        ingest bug). Surface it; do not silently re-sort.
        """
        prev: int | None = None
        for event in events:
            if prev is not None and event.sequence_number <= prev:
                raise ValueError(
                    f"out-of-order sequence_number at boundary "
                    f"prev={prev} current={event.sequence_number} "
                    f"event_id={event.id!r}; segmenter does not "
                    f"silently re-sort"
                )
            prev = event.sequence_number

    @staticmethod
    def _extract_user_prompt(seg_events: list[Event]) -> dict[str, Any] | None:
        """Return a dict view of the segment's USER_PROMPT event, or None."""
        for e in seg_events:
            if e.event_type is EventType.USER_PROMPT:
                return {
                    **e.data,
                    "id": e.id,
                    "sequence_number": e.sequence_number,
                }
        return None

    def _assemble_events(
        self, seg_events: list[Event], exclude_user_prompt: bool
    ) -> list[ToolUseSpan | dict[str, Any]]:
        """Pair tool_use_start with the next matching tool_use_end.

        Pairing key: (tool_name, target). Matched pairs become a single
        ToolUseSpan; un-matched starts surface as orphan spans at the
        end of the pass; un-matched ends surface as 0-width synthesized
        spans inline. All other event types pass through as raw dicts.
        """
        out: list[ToolUseSpan | dict[str, Any]] = []
        # FIFO queue of pending starts keyed by (tool_name, target).
        pending: dict[tuple[str, Any], list[Event]] = defaultdict(list)

        for event in seg_events:
            if (
                exclude_user_prompt
                and event.event_type is EventType.USER_PROMPT
            ):
                continue

            if event.event_type is EventType.TOOL_USE_START:
                key = (
                    str(event.data.get("tool_name", "")),
                    event.data.get("target"),
                )
                pending[key].append(event)
                continue

            if event.event_type is EventType.TOOL_USE_END:
                key = (
                    str(event.data.get("tool_name", "")),
                    event.data.get("target"),
                )
                queue = pending.get(key)
                if queue:
                    start_event = queue.pop(0)
                    if not queue:
                        del pending[key]
                    out.append(self._pair_to_span(start_event, event))
                else:
                    _logger.warning(
                        "orphan_tool_use_end event_id=%r seq=%d session=%r",
                        event.id,
                        event.sequence_number,
                        event.session_id,
                    )
                    out.append(self._synthesize_end_only_span(event))
                continue

            # Pass-through for thinking, response, sub_agent_*,
            # session_*, task_*, etc.
            out.append(self._event_to_dict(event))

        # Any remaining pending starts are orphan; emit at the end of
        # the pass with success=None so the LLM sees them.
        for queue in pending.values():
            for start_event in queue:
                _logger.warning(
                    "orphan_tool_use_start event_id=%r seq=%d session=%r",
                    start_event.id,
                    start_event.sequence_number,
                    start_event.session_id,
                )
                out.append(self._synthesize_start_only_span(start_event))

        # Restore chronological order: orphan starts emitted at the
        # end of the pass need to slot back into their proper position
        # (ToolUseSpan.start_seq vs raw-dict.sequence_number).
        return sorted(out, key=self._sort_key)

    @staticmethod
    def _sort_key(item: ToolUseSpan | dict[str, Any]) -> int:
        """Chronological sort key.

        A raw dict MUST carry `sequence_number` — `_event_to_dict`
        always emits it. A missing key indicates the dict was injected
        from outside the segmenter pipeline; raising surfaces the
        upstream contract break instead of silently coercing the
        unknown-position item to position 0 (which would float it to
        the front of the segment).
        """
        if isinstance(item, ToolUseSpan):
            return item.start_seq
        seq = item.get("sequence_number")
        if seq is None:
            raise ValueError(
                f"raw event dict missing required 'sequence_number' "
                f"key: {item!r}; cannot place in chronological order"
            )
        return int(seq)

    @staticmethod
    def _pair_to_span(start: Event, end: Event) -> ToolUseSpan:
        return ToolUseSpan(
            tool_name=str(start.data.get("tool_name", "")),
            target=start.data.get("target"),
            success=bool(end.data.get("success", False)),
            duration_ms=end.duration_ms,
            start_seq=start.sequence_number,
            end_seq=end.sequence_number,
            metadata={**start.data},
        )

    @staticmethod
    def _synthesize_start_only_span(start: Event) -> ToolUseSpan:
        return ToolUseSpan(
            tool_name=str(start.data.get("tool_name", "")),
            target=start.data.get("target"),
            success=None,
            duration_ms=None,
            start_seq=start.sequence_number,
            end_seq=None,
            metadata={**start.data},
        )

    @staticmethod
    def _synthesize_end_only_span(end: Event) -> ToolUseSpan:
        # End-only synthesis: the end event recorded a duration_ms and
        # success boolean. Trust those, but mark start_seq == end_seq
        # so the contract "we don't know when this started" is visible.
        recorded_success = end.data.get("success")
        if recorded_success is None:
            success: bool | None = None
        else:
            success = bool(recorded_success)
        return ToolUseSpan(
            tool_name=str(end.data.get("tool_name", "")),
            target=end.data.get("target"),
            success=success,
            duration_ms=end.duration_ms,
            start_seq=end.sequence_number,
            end_seq=end.sequence_number,
            metadata={**end.data},
        )

    @staticmethod
    def _event_to_dict(event: Event) -> dict[str, Any]:
        return {
            "id": event.id,
            "session_id": event.session_id,
            "project_id": event.project_id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp.isoformat(),
            "sequence_number": event.sequence_number,
            "segment_index": event.segment_index,
            "sub_agent_id": event.sub_agent_id,
            "depth": event.depth,
            "duration_ms": event.duration_ms,
            "token_count": event.token_count,
            "data": event.data,
        }
