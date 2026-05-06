"""Observation API — schemas + router (task-A3 + task-A5).

This module hosts the Pydantic response shapes AND the FastAPI router that
serves the GUR-107a Observation API (SD §10.4):

    GET  /api/sessions                               → ListSessionsResponse
    GET  /api/sessions/{session_id}                  → SessionDetail
    GET  /api/sessions/{session_id}/segments         → ListSegmentsResponse
    GET  /api/sessions/{session_id}/segments/{idx}   → SegmentDetail

D8 asymmetry note: the API uses the async ``ProjectRegistry`` in-server,
while the cleanup CLI (task-A6) walks the filesystem synchronously. The
two paths are intentional — see ``cli/sync.py:_select_project_ids`` for
the CLI precedent. Server-side request handling stays on the async path
because the registry caches per-project DBEngines for the process
lifetime.

Why these specific fields:
- SessionSummary / SessionDetail expose ``last_event_at`` because the
  retention TTL boundary is computed from ``MAX(events.timestamp)`` per
  session (plan D1). Surfacing it on the read side keeps the dashboard's
  understanding of "is this session expired?" aligned with the cleanup
  worker's understanding (no two sources of truth).
- SegmentSummary surfaces aggregate ``duration_ms`` / ``token_count`` so
  Level 2 drill-down can render without a follow-up Level 3 fetch.
- All shapes are frozen + ``extra="forbid"``: API responses are write-once
  and must not silently accept fields that future schema migrations
  remove.

DC-4 enforcement: every endpoint declares ``project_id`` as ``Query(...)``
with no default. FastAPI returns 422 automatically if it is absent.
There is NO server-side fallback to "first project found" — that would
silently leak data across projects, which is the exact failure DC-4
exists to prevent.

DC-7 baseline: listing endpoints emit an ETag derived from
``MAX(events.timestamp)`` over the relevant scope (project for the
session list, session for the segment list). When the client echoes that
back via ``If-None-Match``, we return 304 with no body. Cursor-based
pagination on top of this lands in task-A7.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, cast

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from secondsight.event import Event, EventType
from secondsight.storage.events_table import events as events_table

if TYPE_CHECKING:
    from secondsight.api.registry import ProjectResources
    from secondsight.api.server import AppState
    from secondsight.storage.events_repository import EventsRepository

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


# ---------------------------------------------------------------------------
# Aggregation queries (task-A5)
# ---------------------------------------------------------------------------
#
# These reach into ``repo._db.engine`` for cross-event aggregation rather
# than going through public EventsRepository methods. The repository's
# public surface is per-event CRUD; layering aggregate read queries onto
# it would couple it to dashboard concerns. The retention module makes
# the same choice for the same reason (see retention.py module note).


def _list_session_summaries(
    repo: EventsRepository,
    project_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list[SessionSummary], int]:
    """Return (summaries, total_count) for the project.

    ``total_count`` is the unsliced number of sessions; the caller uses it
    to decide whether ``next_cursor`` should be set.
    """
    base = (
        sa.select(
            events_table.c.session_id,
            sa.func.min(events_table.c.timestamp).label("first_event_at"),
            sa.func.max(events_table.c.timestamp).label("last_event_at"),
            sa.func.count().label("event_count"),
            sa.func.count(sa.distinct(events_table.c.segment_index)).label("segment_count"),
        )
        .where(events_table.c.project_id == project_id)
        .group_by(events_table.c.session_id)
        .order_by(events_table.c.session_id.asc())
    )

    count_stmt = sa.select(sa.func.count(sa.distinct(events_table.c.session_id))).where(
        events_table.c.project_id == project_id
    )

    paged = base.limit(limit).offset(offset)

    with repo._db.engine.connect() as conn:  # noqa: SLF001
        rows = conn.execute(paged).all()
        total = int(conn.execute(count_stmt).scalar() or 0)

    summaries = [
        SessionSummary(
            session_id=r.session_id,
            project_id=project_id,
            first_event_at=r.first_event_at,
            last_event_at=r.last_event_at,
            event_count=int(r.event_count),
            segment_count=int(r.segment_count),
        )
        for r in rows
    ]
    return summaries, total


def _get_session_detail(
    repo: EventsRepository,
    project_id: str,
    session_id: str,
) -> SessionDetail | None:
    stmt = (
        sa.select(
            sa.func.min(events_table.c.timestamp).label("first_event_at"),
            sa.func.max(events_table.c.timestamp).label("last_event_at"),
            sa.func.count().label("event_count"),
            sa.func.count(sa.distinct(events_table.c.segment_index)).label("segment_count"),
        )
        .where(events_table.c.project_id == project_id)
        .where(events_table.c.session_id == session_id)
    )
    with repo._db.engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(stmt).first()

    # Aggregate over zero rows returns (NULL, NULL, 0, 0) in sqlite —
    # event_count == 0 is the unambiguous "session does not exist" signal.
    if row is None or int(row.event_count) == 0:
        return None

    return SessionDetail(
        session_id=session_id,
        project_id=project_id,
        first_event_at=row.first_event_at,
        last_event_at=row.last_event_at,
        event_count=int(row.event_count),
        segment_count=int(row.segment_count),
    )


def _list_segment_summaries(
    repo: EventsRepository,
    project_id: str,
    session_id: str,
) -> list[SegmentSummary] | None:
    """Return segments for a session, or None if the session does not exist."""
    # First check existence — distinguishes "no segments" (impossible by
    # construction; every event has a segment_index) from "no session".
    detail = _get_session_detail(repo, project_id, session_id)
    if detail is None:
        return None

    stmt = (
        sa.select(
            events_table.c.segment_index,
            sa.func.min(events_table.c.timestamp).label("first_event_at"),
            sa.func.max(events_table.c.timestamp).label("last_event_at"),
            sa.func.count().label("event_count"),
            sa.func.sum(events_table.c.duration_ms).label("duration_ms"),
            sa.func.sum(events_table.c.token_count).label("token_count"),
        )
        .where(events_table.c.project_id == project_id)
        .where(events_table.c.session_id == session_id)
        .group_by(events_table.c.segment_index)
        .order_by(events_table.c.segment_index.asc())
    )
    with repo._db.engine.connect() as conn:  # noqa: SLF001
        rows = conn.execute(stmt).all()

    return [
        SegmentSummary(
            session_id=session_id,
            segment_index=int(r.segment_index),
            first_event_at=r.first_event_at,
            last_event_at=r.last_event_at,
            event_count=int(r.event_count),
            duration_ms=int(r.duration_ms) if r.duration_ms is not None else None,
            token_count=int(r.token_count) if r.token_count is not None else None,
        )
        for r in rows
    ]


def _get_segment_detail(
    repo: EventsRepository,
    project_id: str,
    session_id: str,
    segment_index: int,
) -> SegmentDetail | None:
    stmt = (
        sa.select(events_table)
        .where(events_table.c.project_id == project_id)
        .where(events_table.c.session_id == session_id)
        .where(events_table.c.segment_index == segment_index)
        .order_by(events_table.c.sequence_number.asc())
    )
    with repo._db.engine.connect() as conn:  # noqa: SLF001
        rows = list(conn.execute(stmt).mappings())

    if not rows:
        return None

    import json

    events_list = [
        Event(
            id=r["id"],
            session_id=r["session_id"],
            project_id=r["project_id"],
            event_type=EventType(r["event_type"]),
            timestamp=r["timestamp"],
            sequence_number=r["sequence_number"],
            segment_index=r["segment_index"],
            sub_agent_id=r["sub_agent_id"],
            depth=r["depth"],
            duration_ms=r["duration_ms"],
            token_count=r["token_count"],
            data=json.loads(r["data"]),
        )
        for r in rows
    ]
    return SegmentDetail(
        session_id=session_id,
        segment_index=segment_index,
        events=events_list,
    )


def _project_etag(repo: EventsRepository, project_id: str) -> str | None:
    """Compute an ETag for the project's session listing.

    Derived from ``MAX(events.timestamp)`` + total event count over the
    project. Returning ``None`` when the project has no events lets the
    caller skip emitting an ETag (no session list to short-circuit).
    """
    stmt = sa.select(
        sa.func.max(events_table.c.timestamp),
        sa.func.count(),
    ).where(events_table.c.project_id == project_id)
    with repo._db.engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(stmt).first()

    if row is None or row[1] == 0:
        return None
    return _hash_etag(f"{project_id}|{row[0].isoformat()}|{int(row[1])}")


def _session_etag(repo: EventsRepository, project_id: str, session_id: str) -> str | None:
    stmt = sa.select(
        sa.func.max(events_table.c.timestamp),
        sa.func.count(),
    ).where(
        events_table.c.project_id == project_id,
        events_table.c.session_id == session_id,
    )
    with repo._db.engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(stmt).first()

    if row is None or row[1] == 0:
        return None
    return _hash_etag(f"{project_id}|{session_id}|{row[0].isoformat()}|{int(row[1])}")


def _hash_etag(seed: str) -> str:
    """Stable, short ETag value. Quoted per RFC 7232.

    SHA-1 is used because we only need collision resistance over a
    process's data set, not cryptographic strength. blake2 would also work;
    sticking with stdlib's hashlib default for portability.
    """
    digest = hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f'"{digest[:16]}"'


# ---------------------------------------------------------------------------
# Router (task-A5)
# ---------------------------------------------------------------------------

router = APIRouter()


async def _aresources(request: Request, project_id: str) -> ProjectResources:
    """Resolve per-project resources via the AppState ProjectRegistry.

    The ``project_id`` arrives validated by ``Query(..., min_length=1)``
    so it is always non-empty here. The registry materialises a fresh
    project directory if one does not yet exist; callers handling
    "session not found" cases distinguish that from "project not found"
    via the aggregate row count, not the registry.
    """
    state = cast("AppState", request.app.state.server_state)
    return await state.registry.get(project_id)


@router.get("/api/sessions", response_model=None)
async def list_sessions(
    request: Request,
    response: Response,
    project_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ListSessionsResponse | Response:
    """List sessions for a project.

    DC-4: ``project_id`` is required (no default → FastAPI returns 422).
    DC-7: an ETag is set on every 200 response and a matching
    ``If-None-Match`` returns 304 with no body.
    """
    resources = await _aresources(request, project_id)
    repo = resources.events_repository

    etag = _project_etag(repo, project_id)
    if etag is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    summaries, total = _list_session_summaries(repo, project_id, limit=limit, offset=offset)
    next_cursor: str | None = None
    if offset + len(summaries) < total:
        next_cursor = str(offset + len(summaries))

    if etag is not None:
        response.headers["ETag"] = etag
    return ListSessionsResponse(sessions=summaries, next_cursor=next_cursor)


@router.get("/api/sessions/{session_id}")
async def get_session(
    request: Request,
    session_id: str,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> SessionDetail:
    resources = await _aresources(request, project_id)
    detail = _get_session_detail(resources.events_repository, project_id, session_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"session {session_id!r} not found in project {project_id!r}",
        )
    return detail


@router.get("/api/sessions/{session_id}/segments", response_model=None)
async def list_segments(
    request: Request,
    response: Response,
    session_id: str,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> ListSegmentsResponse | Response:
    resources = await _aresources(request, project_id)
    repo = resources.events_repository

    etag = _session_etag(repo, project_id, session_id)
    if etag is None:
        raise HTTPException(
            status_code=404,
            detail=f"session {session_id!r} not found in project {project_id!r}",
        )

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    segments = _list_segment_summaries(repo, project_id, session_id)
    if segments is None:
        # Race: ETag computed when session existed, then session was reaped.
        raise HTTPException(
            status_code=404,
            detail=f"session {session_id!r} not found in project {project_id!r}",
        )

    response.headers["ETag"] = etag
    return ListSegmentsResponse(segments=segments)


@router.get("/api/sessions/{session_id}/segments/{segment_index}")
async def get_segment(
    request: Request,
    session_id: str,
    segment_index: int,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> SegmentDetail:
    resources = await _aresources(request, project_id)
    detail = _get_segment_detail(resources.events_repository, project_id, session_id, segment_index)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"segment {segment_index} not found in session "
                f"{session_id!r} (project {project_id!r})"
            ),
        )
    return detail


__all__ = [
    "ListSegmentsResponse",
    "ListSessionsResponse",
    "SegmentDetail",
    "SegmentSummary",
    "SessionDetail",
    "SessionSummary",
    "router",
]
