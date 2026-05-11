"""Pydantic response shapes for the Observation API (task-A3, GUR-147).

Per SD §10.4 + memory ``dashboard_api_contracts``: every endpoint
takes ``project_id`` as a required query parameter (D2 in 2-plan.md);
listing endpoints support ``limit``/``offset`` pagination (D6) and
return an ETag-friendly response shape; segment-detail returns full
event payloads while list endpoints stay SQL-only (D7).

These schemas are the *response* shapes. Request inputs flow through
FastAPI ``Query()`` parameters, not Pydantic models, because they are
all simple scalars (project_id, limit, offset).

Decision: ``last_event_at`` and ``first_event_at`` are typed as
``datetime`` and serialised by FastAPI as ISO-8601 strings. The
``events`` table stores naive datetimes (SQLite ``sa.DateTime``);
callers MUST treat unsuffixed timestamps as UTC. We document this
convention here rather than coercing at the schema layer because
coercion would mask the underlying storage convention from any future
analyzer that reads this surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionSummary(BaseModel):
    """One row in ``GET /api/sessions``.

    Computed via aggregate query against ``events`` — no per-event
    JSON parse (D7).
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class ListSessionsResponse(BaseModel):
    """Bounded JSON response for ``GET /api/sessions`` (D6: no
    streaming).

    ``next_cursor`` is non-null when more rows are available beyond
    the requested ``limit``. The cursor is an opaque string the client
    sends back as ``offset`` on the next call. We could embed
    timestamp+session_id, but for single-project MVP traffic
    offset-based is good enough and obviously correct.
    """

    model_config = ConfigDict(extra="forbid")

    sessions: list[SessionSummary]
    next_cursor: str | None = None
    total_count: int | None = Field(
        default=None,
        description=(
            "Optional total count for pagination UIs. Populated on the "
            "first page (offset=0); omitted on subsequent pages to "
            "avoid recomputing a COUNT(DISTINCT session_id) on every "
            "scroll."
        ),
    )


class SessionDetail(BaseModel):
    """Response for ``GET /api/sessions/{session_id}``.

    Same shape as :class:`SessionSummary` for now — included as a
    distinct type so future enrichment (e.g. resolved sub_agent_ids,
    flagged behaviour summaries from GUR-101) can land without
    changing the listing endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class SegmentSummary(BaseModel):
    """One row in ``GET /api/sessions/{session_id}/segments``."""

    model_config = ConfigDict(extra="forbid")

    segment_index: int = Field(ge=0)
    event_count: int = Field(ge=0)
    first_event_at: datetime
    last_event_at: datetime


class ListSegmentsResponse(BaseModel):
    """Bounded JSON response for the per-session segment listing."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    project_id: str
    segments: list[SegmentSummary]


class SegmentEvent(BaseModel):
    """One event in a segment-detail response.

    This is intentionally a thin projection of
    :class:`secondsight.event.Event`: the dashboard renders the event
    timeline, so it needs the typed fields plus the ``data`` payload.
    We do NOT re-export the full ``Event`` because that model lives in
    the ingest path and might pick up adapter-specific fields under
    ``extra=allow`` in the future; the API surface should be stable
    against ingest churn.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    project_id: str
    event_type: str
    timestamp: datetime
    sequence_number: int = Field(ge=0)
    segment_index: int = Field(ge=0)
    sub_agent_id: str | None = None
    depth: int = Field(ge=0)
    duration_ms: int | None = None
    token_count: int | None = None
    data: dict[str, Any]


class SegmentDetail(BaseModel):
    """Response for ``GET /api/sessions/{session_id}/segments/{idx}``.

    Returns the full event list for a single segment. This is the only
    endpoint that opens the ``events.data`` JSON column (D7); listing
    endpoints stay SQL-only.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    project_id: str
    segment_index: int = Field(ge=0)
    events: list[SegmentEvent]


__all__ = [
    "ListSegmentsResponse",
    "ListSessionsResponse",
    "SegmentDetail",
    "SegmentEvent",
    "SegmentSummary",
    "SessionDetail",
    "SessionSummary",
]
