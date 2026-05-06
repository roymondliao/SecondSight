"""Observation API — schemas only (task-A3).

This module hosts the Pydantic response shapes for the GUR-107a Observation API
(SD §10.4):

    GET  /api/sessions                               → ListSessionsResponse
    GET  /api/sessions/{session_id}                  → SessionDetail
    GET  /api/sessions/{session_id}/segments         → ListSegmentsResponse
    GET  /api/sessions/{session_id}/segments/{idx}   → SegmentDetail

The router itself (mounted by `api.server.create_app`) is added in task-A5.
This file is intentionally I/O-free; aggregation lives in the repository
layer (task-A5 wiring).

Why these specific fields:
- SessionSummary / SessionDetail expose `last_event_at` because the retention
  TTL boundary is computed from `MAX(events.timestamp)` per session
  (plan D1). Surfacing it on the read side keeps the dashboard's
  understanding of "is this session expired?" aligned with the cleanup
  worker's understanding (no two sources of truth).
- SegmentSummary surfaces aggregate `duration_ms` / `token_count` so Level 2
  drill-down can render without a follow-up Level 3 fetch.
- All shapes are frozen + extra="forbid": API responses are write-once and
  must not silently accept fields that future schema migrations remove
  (silent drift between server and dashboard is the failure mode this
  guards against).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from secondsight.event import Event

_STRICT = ConfigDict(frozen=True, extra="forbid")


class SessionSummary(BaseModel):
    """List-row shape for `GET /api/sessions`."""

    model_config = _STRICT

    session_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class SessionDetail(BaseModel):
    """Header shape for `GET /api/sessions/{session_id}`.

    Matches SessionSummary today; kept distinct so future fields specific to
    detail view (e.g. agent identifier, schema_version) can be added without
    bloating list responses.
    """

    model_config = _STRICT

    session_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class SegmentSummary(BaseModel):
    """List-row shape for `GET /api/sessions/{session_id}/segments`."""

    model_config = _STRICT

    session_id: str = Field(min_length=1, max_length=128)
    segment_index: int = Field(ge=0)
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    token_count: int | None = Field(default=None, ge=0)


class SegmentDetail(BaseModel):
    """Full event timeline for `GET /api/sessions/{session_id}/segments/{idx}`."""

    model_config = _STRICT

    session_id: str = Field(min_length=1, max_length=128)
    segment_index: int = Field(ge=0)
    events: list[Event]


class ListSessionsResponse(BaseModel):
    """Envelope for the paginated session listing.

    `next_cursor` is None when the listing is exhausted. Cursor format is an
    opaque string owned by the router (task-A7); schema layer does not parse
    it.
    """

    model_config = _STRICT

    sessions: list[SessionSummary]
    next_cursor: str | None = None


class ListSegmentsResponse(BaseModel):
    """Envelope for segment listing — no pagination (segments per session are
    bounded by ~10s of integer indexes; full enumeration is fine)."""

    model_config = _STRICT

    segments: list[SegmentSummary]


__all__ = [
    "ListSegmentsResponse",
    "ListSessionsResponse",
    "SegmentDetail",
    "SegmentSummary",
    "SessionDetail",
    "SessionSummary",
]
