"""Supplementary metrics — pure-function per-segment metrics (GUR-100 task-5).

Per SD §5.3.1 step 2: cheap context fed to the LLM analysis prompt
alongside the segment events. The metrics are NOT used for autonomous
filtering decisions; they are advisory inputs to the LLM.

Death cases enforced here:
- Null token_count contributes 0 with a WARNING log when emitted by an
  event type expected to carry tokens (thinking, response). NEVER raises;
  NEVER silently coerces without trace.
- Empty segment returns the all-zero baseline; NEVER raises.
- ToolUseSpan(success=None) does NOT count as error; only success=False
  does. "Unknown" is not "failed".
"""

from __future__ import annotations

import logging
from datetime import datetime

from secondsight.analysis.schemas import (
    SegmentData,
    SegmentMetrics,
    ToolUseSpan,
)

_logger = logging.getLogger(__name__)

# Tools that touch a file on the filesystem. unique_files counts
# distinct `target` values across these tool names. Configurable as
# a module-level constant rather than a function argument so the
# metric is deterministic across callers.
FILE_TOUCHING_TOOLS: frozenset[str] = frozenset(
    {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"}
)

# Event types that are expected to carry a non-None token_count.
# A null token_count on these is a data-quality signal worth logging.
# Other event types (session_start, tool_use_start, etc.) legitimately
# carry None and emit no warning.
_TOKEN_BEARING_EVENT_TYPES: frozenset[str] = frozenset(
    {"thinking", "response"}
)


def compute_segment_metrics(segment: SegmentData) -> SegmentMetrics:
    """Pure function. No DB. Side effect: WARNING log on null token_count
    for an event type that is expected to carry one.
    """
    if not segment.events:
        return SegmentMetrics(
            total_tokens=0, unique_files=0, duration=0.0, error_count=0
        )

    total_tokens = 0
    unique_files: set[str] = set()
    error_count = 0
    timestamps: list[datetime] = []

    for item in segment.events:
        if isinstance(item, ToolUseSpan):
            if (
                item.tool_name in FILE_TOUCHING_TOOLS
                and item.target is not None
            ):
                unique_files.add(item.target)
            # Only success=False counts as error. success=None (orphan)
            # is "unknown", not "failed".
            if item.success is False:
                error_count += 1
            # Tool spans don't carry top-level timestamps; the surrounding
            # raw events anchor the segment timeline.
            continue

        # Raw event dict (thinking / response / sub_agent_* / session_*).
        tc = item.get("token_count")
        if tc is None:
            event_type = item.get("event_type")
            if event_type in _TOKEN_BEARING_EVENT_TYPES:
                _logger.warning(
                    "null token_count on event_id=%r event_type=%r — "
                    "contributes 0 to total_tokens",
                    item.get("id"),
                    event_type,
                )
            # Else: legitimately null (session_start etc.); no warning.
        else:
            total_tokens += int(tc)

        ts = item.get("timestamp")
        if isinstance(ts, str):
            try:
                timestamps.append(datetime.fromisoformat(ts))
            except ValueError:
                _logger.warning(
                    "unparseable timestamp on event_id=%r ts=%r — "
                    "excluded from duration calculation",
                    item.get("id"),
                    ts,
                )
        elif isinstance(ts, datetime):
            timestamps.append(ts)

    if len(timestamps) <= 1:
        duration = 0.0
    else:
        duration = (max(timestamps) - min(timestamps)).total_seconds()

    return SegmentMetrics(
        total_tokens=total_tokens,
        unique_files=len(unique_files),
        duration=duration,
        error_count=error_count,
    )
