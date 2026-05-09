"""Analysis API — 6 GET endpoints (GUR-104 task-3).

Endpoints:
- GET /api/analysis/summary — single-object project rollup
- GET /api/analysis/sessions — paginated session-analysis list
- GET /api/analysis/sessions/{id} — full per-session report
- GET /api/analysis/sessions/{id}/flags — flags-only view
- GET /api/analysis/trends — per-session flag-type breakdown
- GET /api/analysis/aggregation — cross-session statistics

Convention reuse (api/observation.py): frozen Pydantic + extra=forbid,
required project_id Query, weak ETag from MAX(updated_at) over scope,
is_safe_id on path params, 404 (not 200+empty) on by-id miss.

Death-case defenses pinned by acceptance.yaml:
- DC-1: cross-project mismatch (project_id=A, session in project B) → 404
- DC-3: ETag scope spans all 4 tables (flags + directives + reports + runs)
  for the summary endpoint; missing a table = stale-cache silent fail
- DC-6: missing session_reports row → 404, never 200+empty
- DC-7: trends LIMIT applies to session set (delegated to task-1's
  count_per_session_for_project), not joined flags rows
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from secondsight.analysis.schemas import BehaviorFlag, BehaviorFlagType
from secondsight.api._id_safety import is_safe_id
from secondsight.api.registry import ProjectResources
from secondsight.api.server import AppState
from secondsight.storage.analysis_runs_table import analysis_runs as analysis_runs_table
from secondsight.storage.behavior_flags_repository import (
    BehaviorFlagsRepository,
)
from secondsight.storage.behavior_flags_table import behavior_flags as behavior_flags_table
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.directives_table import directives as directives_table
from secondsight.storage.session_reports_repository import (
    SessionReportsRepository,
)
from secondsight.storage.session_reports_table import session_reports as session_reports_table

_STRICT = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class BehaviorFlagOut(BaseModel):
    """Response shape mirroring ``analysis.schemas.BehaviorFlag``.

    `confidence` is mandatory per memory contract
    `behaviorflag_schema_contract` — without it the dashboard cannot
    triage low-confidence flags.
    """

    model_config = _STRICT

    id: str
    project_id: str
    session_id: str
    segment_index: int
    flag_type: str
    event_ids: list[str]
    intent_summary: str
    reason: str
    confidence: Literal["high", "medium", "low"]
    created_at: datetime

    @classmethod
    def from_flag(cls, flag: BehaviorFlag) -> "BehaviorFlagOut":
        return cls(
            id=flag.id,
            project_id=flag.project_id,
            session_id=flag.session_id,
            segment_index=flag.segment_index,
            flag_type=flag.flag_type.value,
            event_ids=list(flag.event_ids),
            intent_summary=flag.intent_summary,
            reason=flag.reason,
            confidence=flag.confidence,
            created_at=flag.created_at,
        )


class AnalysisSummary(BaseModel):
    """Project-level rollup. Field set per `2-plan.md` G2 lock."""

    model_config = _STRICT

    project_id: str
    analyzed_session_count: int = Field(ge=0)
    flag_counts_by_type: dict[str, int]
    active_directive_count: int = Field(ge=0)
    last_analyzed_at: datetime | None = None
    as_of: datetime


class SessionAnalysisItem(BaseModel):
    model_config = _STRICT

    session_id: str
    analyzed_at: datetime
    headline: str
    flag_count: int = Field(ge=0)
    key_findings: list[str] = Field(default_factory=list)


class ListSessionsResponse(BaseModel):
    model_config = _STRICT

    project_id: str
    items: list[SessionAnalysisItem]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    next_offset: int | None = None


class SessionAnalysisDetail(BaseModel):
    model_config = _STRICT

    project_id: str
    session_id: str
    headline: str
    body: str
    key_findings: list[str] = Field(default_factory=list)
    analyzed_at: datetime
    flags: list[BehaviorFlagOut]


class TrendsBucket(BaseModel):
    model_config = _STRICT

    session_id: str
    analyzed_at: datetime
    counts_by_type: dict[str, int]


class TrendsResponse(BaseModel):
    model_config = _STRICT

    project_id: str
    buckets: list[TrendsBucket]


class AggregationResponse(BaseModel):
    model_config = _STRICT

    project_id: str
    flag_counts_by_type: dict[str, int]
    session_counts_by_type: dict[str, int]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_etag(seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f'W/"{digest[:16]}"'


def _summary_etag(db_engine, project_id: str) -> str | None:
    """ETag spanning all 4 analysis tables for the project (DC-3).

    Missing a table here = silent stale cache. The seed includes
    MAX(updated_at) per table + total row count; any insert/update in
    the project's analysis state invalidates.
    """
    parts: list[str] = [project_id]
    total = 0
    for table, ts_col in (
        (behavior_flags_table, behavior_flags_table.c.created_at),
        (directives_table, directives_table.c.updated_at),
        (session_reports_table, session_reports_table.c.updated_at),
        (analysis_runs_table, analysis_runs_table.c.updated_at),
    ):
        with db_engine.engine.connect() as conn:
            row = conn.execute(
                sa.select(sa.func.max(ts_col), sa.func.count()).where(
                    table.c.project_id == project_id
                )
            ).first()
        if row is None:
            parts.append("none|0")
            continue
        ts, count = row
        parts.append(
            f"{ts.isoformat() if ts is not None else 'none'}|{int(count)}"
        )
        total += int(count)

    if total == 0:
        return None
    return _hash_etag("|".join(parts))


def _list_etag(db_engine, project_id: str, table, ts_col) -> str | None:
    """Generic single-table ETag for listing endpoints."""
    with db_engine.engine.connect() as conn:
        row = conn.execute(
            sa.select(sa.func.max(ts_col), sa.func.count()).where(
                table.c.project_id == project_id
            )
        ).first()
    if row is None or int(row[1]) == 0:
        return None
    return _hash_etag(
        f"{project_id}|{row[0].isoformat()}|{int(row[1])}"
    )


def _ensure_phase2_schemas(resources: ProjectResources) -> None:
    """Idempotent create_schema for all 4 Phase 2 tables.

    A fresh project (Phase 1 ingest happened, Phase 2 never ran) has no
    behavior_flags / directives / session_reports / analysis_runs tables;
    selecting from them would 500 with 'no such table'. checkfirst=True
    makes this a ~1ms metadata round-trip when tables already exist.
    """
    BehaviorFlagsRepository(resources.db_engine).create_schema()
    DirectivesRepository(resources.db_engine).create_schema()
    SessionReportsRepository(resources.db_engine).create_schema()
    # analysis_runs has no public Repository.create_schema() of its own
    # in some branches — use the table's metadata directly.
    analysis_runs_table.metadata.create_all(
        resources.db_engine.engine, checkfirst=True
    )


async def _aresources(request: Request, project_id: str) -> ProjectResources:
    if not is_safe_id(project_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"project_id {project_id!r} contains unsafe characters."
            ),
        )
    state: AppState = request.app.state.server_state
    return await state.registry.get(project_id)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter()


@router.get("/api/analysis/summary", response_model=None)
async def analysis_summary(
    request: Request,
    response: Response,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> AnalysisSummary | Response:
    """Project-level analysis rollup.

    DC-3 defense: ETag spans all 4 tables. A change in any of
    behavior_flags / directives / session_reports / analysis_runs
    invalidates the cache.
    """
    resources = await _aresources(request, project_id)
    _ensure_phase2_schemas(resources)

    etag = _summary_etag(resources.db_engine, project_id)
    if etag is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    # analyzed_session_count = COUNT(session_reports.session_id)
    with resources.db_engine.engine.connect() as conn:
        analyzed_count = int(
            conn.execute(
                sa.select(sa.func.count()).where(
                    session_reports_table.c.project_id == project_id
                )
            ).scalar()
            or 0
        )
        # last_analyzed_at = MAX(session_reports.created_at)
        last_analyzed_at = conn.execute(
            sa.select(sa.func.max(session_reports_table.c.created_at)).where(
                session_reports_table.c.project_id == project_id
            )
        ).scalar()
        # active directive count
        active_directive_count = int(
            conn.execute(
                sa.select(sa.func.count())
                .where(directives_table.c.project_id == project_id)
                .where(directives_table.c.status == "active")
            ).scalar()
            or 0
        )

    # flag_counts_by_type via the existing repo helper.
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    counts = flags_repo.count_by_type(project_id)
    flag_counts_by_type = {ft.value: c for ft, c in counts.items()}

    summary = AnalysisSummary(
        project_id=project_id,
        analyzed_session_count=analyzed_count,
        flag_counts_by_type=flag_counts_by_type,
        active_directive_count=active_directive_count,
        last_analyzed_at=last_analyzed_at,
        as_of=datetime.now(),
    )

    if etag is not None:
        response.headers["ETag"] = etag
    return summary


@router.get("/api/analysis/sessions", response_model=None)
async def list_analyzed_sessions(
    request: Request,
    response: Response,
    project_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ListSessionsResponse | Response:
    """Paginated list of sessions that have a session_reports row."""
    resources = await _aresources(request, project_id)
    _ensure_phase2_schemas(resources)

    etag = _list_etag(
        resources.db_engine,
        project_id,
        session_reports_table,
        session_reports_table.c.updated_at,
    )
    if etag is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    reports_repo = SessionReportsRepository(resources.db_engine)
    reports = reports_repo.list_for_project(
        project_id, limit=limit, offset=offset
    )

    # Per-session flag count via a single GROUP BY.
    items: list[SessionAnalysisItem] = []
    if reports:
        session_ids = [r.session_id for r in reports]
        with resources.db_engine.engine.connect() as conn:
            counts_rows = conn.execute(
                sa.select(
                    behavior_flags_table.c.session_id,
                    sa.func.count(),
                )
                .where(behavior_flags_table.c.project_id == project_id)
                .where(behavior_flags_table.c.session_id.in_(session_ids))
                .group_by(behavior_flags_table.c.session_id)
            ).all()
        count_by_session: dict[str, int] = {
            r[0]: int(r[1]) for r in counts_rows
        }
        items = [
            SessionAnalysisItem(
                session_id=r.session_id,
                analyzed_at=r.created_at,
                headline=r.headline,
                flag_count=count_by_session.get(r.session_id, 0),
                key_findings=list(r.key_findings),
            )
            for r in reports
        ]

    # next_offset present if a full page came back; client decides when
    # to stop polling. Cheaper than a COUNT(*) for the typical case.
    next_offset = (
        offset + len(items) if len(items) == limit else None
    )

    if etag is not None:
        response.headers["ETag"] = etag
    return ListSessionsResponse(
        project_id=project_id,
        items=items,
        limit=limit,
        offset=offset,
        next_offset=next_offset,
    )


@router.get(
    "/api/analysis/sessions/{session_id}", response_model=None
)
async def session_analysis_detail(
    request: Request,
    session_id: str,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> SessionAnalysisDetail:
    """Full session report + flags. 404 on missing report or
    cross-project mismatch (DC-1, DC-6)."""
    if not is_safe_id(session_id):
        raise HTTPException(
            status_code=422,
            detail=f"session_id {session_id!r} contains unsafe characters.",
        )
    resources = await _aresources(request, project_id)
    _ensure_phase2_schemas(resources)
    reports_repo = SessionReportsRepository(resources.db_engine)
    report = reports_repo.get_for_session(session_id)
    if report is None or report.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"session {session_id!r} not analyzed in project "
                f"{project_id!r} (no session_reports row)."
            ),
        )
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    flags = flags_repo.get_session_flags(session_id)
    # Belt-and-braces: filter flags by project_id even though session
    # ids are unique. Defends against a future refactor that introduces
    # session_id collisions across projects.
    flags = [f for f in flags if f.project_id == project_id]
    return SessionAnalysisDetail(
        project_id=project_id,
        session_id=session_id,
        headline=report.headline,
        body=report.body,
        key_findings=list(report.key_findings),
        analyzed_at=report.created_at,
        flags=[BehaviorFlagOut.from_flag(f) for f in flags],
    )


@router.get(
    "/api/analysis/sessions/{session_id}/flags",
    response_model=None,
)
async def session_flags(
    request: Request,
    session_id: str,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> list[BehaviorFlagOut]:
    """Flags-only view of a session's analysis."""
    if not is_safe_id(session_id):
        raise HTTPException(
            status_code=422,
            detail=f"session_id {session_id!r} contains unsafe characters.",
        )
    resources = await _aresources(request, project_id)
    _ensure_phase2_schemas(resources)
    # DC-1 / DC-6: confirm the session was analyzed in THIS project.
    reports_repo = SessionReportsRepository(resources.db_engine)
    report = reports_repo.get_for_session(session_id)
    if report is None or report.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"session {session_id!r} not analyzed in project "
                f"{project_id!r}."
            ),
        )
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    flags = [
        f
        for f in flags_repo.get_session_flags(session_id)
        if f.project_id == project_id
    ]
    return [BehaviorFlagOut.from_flag(f) for f in flags]


@router.get("/api/analysis/trends", response_model=None)
async def analysis_trends(
    request: Request,
    project_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=200),
) -> TrendsResponse:
    """Per-session flag-type breakdown — DC-7 defense delegated to
    BehaviorFlagsRepository.count_per_session_for_project."""
    resources = await _aresources(request, project_id)
    _ensure_phase2_schemas(resources)
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    breakdowns = flags_repo.count_per_session_for_project(
        project_id, limit=limit
    )
    return TrendsResponse(
        project_id=project_id,
        buckets=[
            TrendsBucket(
                session_id=b.session_id,
                analyzed_at=b.analyzed_at,
                counts_by_type={
                    ft.value: c for ft, c in b.counts_by_type.items()
                },
            )
            for b in breakdowns
        ],
    )


@router.get("/api/analysis/aggregation", response_model=None)
async def analysis_aggregation(
    request: Request,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> AggregationResponse:
    """Cross-session statistics — flag count + distinct-session count
    per flag type."""
    resources = await _aresources(request, project_id)
    _ensure_phase2_schemas(resources)
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    counts = flags_repo.count_by_type(project_id)
    flag_counts_by_type = {ft.value: c for ft, c in counts.items()}

    # session_counts_by_type via SELECT COUNT(DISTINCT session_id)
    # GROUP BY flag_type
    with resources.db_engine.engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                behavior_flags_table.c.flag_type,
                sa.func.count(sa.distinct(behavior_flags_table.c.session_id)),
            )
            .where(behavior_flags_table.c.project_id == project_id)
            .group_by(behavior_flags_table.c.flag_type)
        ).all()
    session_counts_by_type: dict[str, int] = {}
    for raw_type, cnt in rows:
        try:
            BehaviorFlagType(raw_type)  # validate
            session_counts_by_type[raw_type] = int(cnt)
        except ValueError:
            # Out-of-vocabulary flag_type rows are skipped (mirrors
            # count_by_type's precedent).
            continue

    return AggregationResponse(
        project_id=project_id,
        flag_counts_by_type=flag_counts_by_type,
        session_counts_by_type=session_counts_by_type,
    )


__all__ = [
    "AggregationResponse",
    "AnalysisSummary",
    "BehaviorFlagOut",
    "ListSessionsResponse",
    "SessionAnalysisDetail",
    "SessionAnalysisItem",
    "TrendsBucket",
    "TrendsResponse",
    "router",
]
