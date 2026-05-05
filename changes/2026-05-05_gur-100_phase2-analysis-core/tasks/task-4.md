# Task 4: Segmenter — read-side event assembler

## Context

Read: `overview.md` for full architecture. Task-1 must be complete:
this task imports `SegmentData`, `ToolUseSpan` from
`secondsight.analysis.schemas`.

**This is an ASSEMBLER, not a re-segmenter.** Events are already
segmented at ingest by `SessionTracker.bind()`
(`src/secondsight/observation/tracker.py:193-195` increments
`segment_index` only on `USER_PROMPT` events). The segmenter's job
is to:

1. Read all events for a session via `EventsRepository.get_session_events()`.
2. Group by `segment_index`.
3. Within each segment, pair `tool_use_start` events with the
   subsequent matching `tool_use_end` event into `ToolUseSpan`
   instances. Other events (thinking, response, sub_agent_*) pass
   through as raw dicts.
4. Output `list[SegmentData]`, one per `segment_index`, including
   the implicit `segment_index=0` pre-prompt segment if non-empty.

**Death cases — never silently:**

- Orphan `tool_use_start` (no matching end) → `ToolUseSpan(success=None,
  duration_ms=None, end_seq=None)`. Span is emitted, not omitted.
- Orphan `tool_use_end` (no preceding start) → synthesized
  `ToolUseSpan(start_seq=event.sequence_number,
  end_seq=event.sequence_number, success=event.data.get("success"),
  duration_ms=event.duration_ms)` with WARNING log.
- Empty segment (`USER_PROMPT` immediately followed by next
  `USER_PROMPT`) → `SegmentData(events=[])`. Segment exists.
- Pre-prompt events (`segment_index=0` with no triggering USER_PROMPT)
  → `SegmentData(user_prompt=None, events=[...])`. Segment exists.
- Out-of-order `sequence_number` within a session → raise `ValueError`.
  Do NOT silently re-sort.

## Files

- Create: `src/secondsight/analysis/segmenter.py`
- Create: `tests/analysis/test_segmenter.py`
- Modify: `src/secondsight/analysis/__init__.py` to add `Segmenter` to
  `__all__` exports

## Death Test Requirements

Write these BEFORE implementation. Each uses a synthetic event list
fed via a stub `EventsRepository` so tests don't need a real DB.

- **DT-4.1** — Orphan `tool_use_start` at seq=5 (no matching end in
  the session) → output contains `ToolUseSpan(start_seq=5,
  end_seq=None, success=None, duration_ms=None)`. The test fails if
  the orphan span is missing from the output.
- **DT-4.2** — Orphan `tool_use_end` at seq=7 (no preceding start) →
  output contains synthesized `ToolUseSpan(start_seq=7, end_seq=7)`.
  WARNING log captured (use `caplog` or similar) naming
  `"orphan_tool_use_end"`.
- **DT-4.3** — Pre-prompt events: session contains `session_start`
  (seq=1, segment_index=0) before any USER_PROMPT, then USER_PROMPT
  (seq=2, segment_index=1) → output has 2 SegmentData entries:
  `[SegmentData(segment_index=0, user_prompt=None, events=[<session_start dict>]),
  SegmentData(segment_index=1, user_prompt=<USER_PROMPT event dict>, events=[])]`.
- **DT-4.4** — Empty segment: USER_PROMPT (seq=2, idx=1) →
  USER_PROMPT (seq=3, idx=2) → `tool_use_start`/`end` (seq=4-5, idx=2)
  → output has 2 SegmentData. The first (idx=1) has `events=[]`. NOT
  silently merged or dropped.
- **DT-4.5** — Out-of-order sequence_number ([1, 3, 2, 4]) →
  `ValueError` raised, message names the violating boundary.
- **DT-4.6** — Successful pairing: `tool_use_start(seq=4,
  data={tool_name:"Read", target:"/x.py"})` → `tool_use_end(seq=5,
  duration_ms=120, data={success:true})` → `ToolUseSpan(tool_name="Read",
  target="/x.py", success=True, duration_ms=120, start_seq=4, end_seq=5)`.
- **DT-4.7** — Sub-agent passthrough: `sub_agent_start(seq=3)` and
  `sub_agent_end(seq=8)` are present in the segment's `events` list
  as raw dicts. They are NOT consumed or transformed; only tool-use
  pairs are.

## Implementation Steps

- [ ] Step 1: Write `tests/analysis/test_segmenter.py` with DT-4.1..DT-4.7
      plus 1 happy-path test (8-event session: start →
      USER_PROMPT(1) → thinking → tool_start → tool_end →
      USER_PROMPT(2) → tool_start → tool_end → end → assert 3
      SegmentData with segment_index 0/1/2 and 2 paired ToolUseSpans).
- [ ] Step 2: Run tests — all red.
- [ ] Step 3: Implement `Segmenter` per the spec below.
- [ ] Step 4: Run tests — all green.
- [ ] Step 5: Run full test suite — no regressions.
- [ ] Step 6: Write scar report. Commit.

## Spec — `src/secondsight/analysis/segmenter.py`

```python
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

Death cases (NEVER silently):
- Orphan tool_use_start → ToolUseSpan(success=None, end_seq=None).
- Orphan tool_use_end → synthesized ToolUseSpan with start_seq==end_seq
  and a WARNING log.
- Out-of-order sequence_number → raise ValueError.
- Pre-prompt segment (segment_index=0 with no USER_PROMPT) → emitted
  with user_prompt=None.
- Empty segment (USER_PROMPT followed immediately by next USER_PROMPT) →
  emitted with events=[].
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
    def __init__(self, events_repo: EventsRepository) -> None:
        self._events_repo = events_repo

    def segment_session(self, session_id: str) -> list[SegmentData]:
        events = self._events_repo.get_session_events(session_id)
        if not events:
            return []

        self._validate_sequence_order(events)

        # Group by segment_index, preserving in-segment order
        # (events are already sorted by sequence_number from get_session_events).
        by_segment: dict[int, list[Event]] = defaultdict(list)
        for event in events:
            by_segment[event.segment_index].append(event)

        # Determine session-wide project_id for each segment from its
        # first event (all events in a session share project_id).
        project_id = events[0].project_id

        segments: list[SegmentData] = []
        for segment_index in sorted(by_segment):
            seg_events = by_segment[segment_index]
            user_prompt = self._extract_user_prompt(seg_events)
            assembled = self._assemble_events(seg_events, exclude_user_prompt=True)
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
        prev = None
        for event in events:
            if prev is not None and event.sequence_number <= prev:
                raise ValueError(
                    f"out-of-order sequence_number at boundary "
                    f"prev={prev} current={event.sequence_number} "
                    f"event_id={event.id!r}; segmenter does not silently re-sort"
                )
            prev = event.sequence_number

    @staticmethod
    def _extract_user_prompt(seg_events: list[Event]) -> dict | None:
        for e in seg_events:
            if e.event_type is EventType.USER_PROMPT:
                return {**e.data, "id": e.id, "sequence_number": e.sequence_number}
        return None

    def _assemble_events(
        self, seg_events: list[Event], exclude_user_prompt: bool
    ) -> list[dict | ToolUseSpan]:
        """Pair tool_use_start with the next tool_use_end carrying the
        same tool_name + target signature within the segment. Pass
        through every other event as a raw dict.
        """
        out: list[dict | ToolUseSpan] = []
        # Index pending tool starts by (tool_name, target) → (start_event, idx_in_out)
        pending_starts: dict[tuple[str, str | None], list[Event]] = defaultdict(list)

        for event in seg_events:
            if exclude_user_prompt and event.event_type is EventType.USER_PROMPT:
                continue

            if event.event_type is EventType.TOOL_USE_START:
                # Hold for pairing; do NOT emit yet.
                key = (
                    str(event.data.get("tool_name", "")),
                    event.data.get("target"),
                )
                pending_starts[key].append(event)
                continue

            if event.event_type is EventType.TOOL_USE_END:
                key = (
                    str(event.data.get("tool_name", "")),
                    event.data.get("target"),
                )
                queue = pending_starts.get(key)
                if queue:
                    start_event = queue.pop(0)
                    if not queue:
                        del pending_starts[key]
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

            # Pass-through for thinking, response, sub_agent_*, session_*, etc.
            out.append(self._event_to_dict(event))

        # Any remaining pending_starts are orphan starts — emit as
        # ToolUseSpan(end_seq=None, success=None).
        for queue in pending_starts.values():
            for start_event in queue:
                _logger.warning(
                    "orphan_tool_use_start event_id=%r seq=%d session=%r",
                    start_event.id,
                    start_event.sequence_number,
                    start_event.session_id,
                )
                out.append(self._synthesize_start_only_span(start_event))

        # Re-sort by start_seq / event sequence_number so output is
        # deterministic. (Orphan starts emitted at end above must be
        # interleaved back into chronological order.)
        return sorted(out, key=self._sort_key)

    @staticmethod
    def _sort_key(item: dict | ToolUseSpan) -> int:
        if isinstance(item, ToolUseSpan):
            return item.start_seq
        return int(item.get("sequence_number", 0))

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
        return ToolUseSpan(
            tool_name=str(end.data.get("tool_name", "")),
            target=end.data.get("target"),
            success=bool(end.data.get("success", False)) if end.data.get("success") is not None else None,
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
```

## Expected Scar Report Items

- Potential shortcut: pair `tool_use_start` with the **next**
  `tool_use_end` regardless of `tool_name`+`target` match. Could
  produce wrong pairs when sub-agents interleave. Rejected; key by
  (tool_name, target).
- Potential shortcut: silently sort events on out-of-order detection
  "to be helpful". Rejected — DT-4.5 closes this.
- Potential shortcut: emit only paired tool-uses, drop orphans
  "because they're noise". Rejected — DT-4.1 / DT-4.2 close this.
- Assumption to verify: `Event.event_type` is an `EventType` enum
  (not raw string) after `events_repository.get_session_events`.
  Verified by reading events_repository._row_to_event.
- Assumption to verify: `target` may be None for some tool types
  (e.g. Bash with command but no file target). Schema accepts None.

## Acceptance Criteria

Covers the following acceptance.yaml scenarios:
- "Silent failure - segmenter drops orphan tool_use_start"
- "Silent failure - segmenter drops orphan tool_use_end"
- "Silent failure - pre-prompt segment merged into segment 1"
- "Silent failure - empty segment dropped from output"
- "Silent failure - segmenter silently sorts out-of-order events"
- "Unknown outcome - tool execution outcome not recorded"
- "Success - segment_session produces SD 5.5.2-shaped output"
