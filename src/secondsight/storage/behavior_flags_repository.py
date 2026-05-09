"""BehaviorFlagsRepository — SQLAlchemy Core repository (GUR-100 task-2).

Idempotency contract:
    insert(flag) is idempotent on `id`. Two calls with the same id and
    different fields persist only the FIRST (ON CONFLICT DO NOTHING).

Defensive enum guard (D1):
    BehaviorFlag.model_construct() bypasses Pydantic. The repository
    re-validates `flag_type` and `confidence` on insert to close that
    silent-failure surface. Without this guard, an analyzer that
    constructs flags via model_construct (perf shortcut) could land
    flag_type='bogus' rows in the DB, silently breaking SD §5.5.1's
    "single source of truth" promise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.analysis.schemas import VALID_CONFIDENCE, BehaviorFlag, BehaviorFlagType
from secondsight.storage.behavior_flags_table import behavior_flags, metadata
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.session_reports_table import session_reports


@dataclass(frozen=True)
class SessionFlagBreakdown:
    """Per-session flag-type breakdown returned by
    count_per_session_for_project. Frozen so callers cannot mutate the
    rolled-up counts after the repository hands them out.
    """

    session_id: str
    analyzed_at: datetime
    counts_by_type: dict[BehaviorFlagType, int]

_logger = logging.getLogger(__name__)


class BehaviorFlagsRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create behavior_flags + indexes if absent. Idempotent."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def insert(self, flag: BehaviorFlag) -> None:
        """Insert one flag. Idempotent on `id`.

        Raises ValueError when flag_type or confidence fail the
        defensive guard (model_construct bypass).
        """
        self._guard(flag)
        row = self._flag_to_row(flag)
        stmt = (
            sqlite_insert(behavior_flags)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

    def insert_many(self, flags: Sequence[BehaviorFlag]) -> int:
        """Insert many flags. Returns `len(flags)` — the INPUT count.

        IMPORTANT — return value semantics (matches the project-wide
        convention from EventsRepository.insert_many):

            return value == len(input)  # ALWAYS

            return value != count of rows newly persisted, because
            ON CONFLICT(id) DO NOTHING silently skips duplicates.

        Callers MUST NOT use this return as an "all flags persisted"
        confirmation. To verify persistence after a batch, query the
        relevant get_session_flags / count_by_type helpers.

        Each flag is re-validated by the defensive guard before any
        SQL is executed; a single bad flag aborts the whole batch
        with ValueError (caller can retry without it).
        """
        if not flags:
            return 0
        for flag in flags:
            self._guard(flag)
        rows = [self._flag_to_row(f) for f in flags]
        stmt = sqlite_insert(behavior_flags).on_conflict_do_nothing(
            index_elements=["id"]
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt, rows)
        return len(rows)

    def get_session_flags(self, session_id: str) -> list[BehaviorFlag]:
        stmt = (
            sa.select(behavior_flags)
            .where(behavior_flags.c.session_id == session_id)
            .order_by(behavior_flags.c.created_at.asc())
        )
        with self._db.engine.connect() as conn:
            return [
                self._row_to_flag(r) for r in conn.execute(stmt).mappings()
            ]

    def get_project_flags_by_type(
        self, project_id: str, flag_type: BehaviorFlagType
    ) -> list[BehaviorFlag]:
        stmt = sa.select(behavior_flags).where(
            sa.and_(
                behavior_flags.c.project_id == project_id,
                behavior_flags.c.flag_type == flag_type.value,
            )
        )
        with self._db.engine.connect() as conn:
            return [
                self._row_to_flag(r) for r in conn.execute(stmt).mappings()
            ]

    def count_by_type(self, project_id: str) -> dict[BehaviorFlagType, int]:
        """Aggregate counts per flag_type for a project.

        A row whose flag_type is outside the enum (only possible if a
        manual SQL edit corrupted the table) is logged and skipped —
        we surface it via WARNING rather than crash, but never
        silently fold it into a sibling enum value.
        """
        stmt = (
            sa.select(behavior_flags.c.flag_type, sa.func.count())
            .where(behavior_flags.c.project_id == project_id)
            .group_by(behavior_flags.c.flag_type)
        )
        with self._db.engine.connect() as conn:
            counts: dict[BehaviorFlagType, int] = {}
            for row in conn.execute(stmt):
                try:
                    counts[BehaviorFlagType(row[0])] = int(row[1])
                except ValueError:
                    _logger.warning(
                        "behavior_flags.flag_type=%r outside enum; "
                        "skipping in count_by_type for project_id=%r",
                        row[0],
                        project_id,
                    )
            return counts

    def count_per_session_for_project(
        self,
        project_id: str,
        *,
        limit: int = 50,
    ) -> list[SessionFlagBreakdown]:
        """Per-session flag-type counts for the most-recently analyzed
        sessions in the project. Backs `GET /api/analysis/trends`
        (per-session) and the cross-session piece of
        `GET /api/analysis/aggregation`.

        DC-7 defense: the LIMIT is applied to the SESSION SET (a
        SUBQUERY against ``session_reports``) BEFORE the LEFT JOIN to
        ``behavior_flags``. A naive ``JOIN ... LIMIT N`` would return
        N flag rows, not N sessions, silently corrupting the trends
        chart with off-by-window pagination. The CTE-style structure
        below makes the right thing the only thing.

        LEFT JOIN preserves zero-flag sessions: a session whose
        ``session_reports`` row exists but has zero behavior_flags
        appears in the result with an empty ``counts_by_type`` dict.
        Switching to INNER JOIN would silently drop those sessions
        from trends — the dashboard would render a chart that pretends
        the session never existed.

        Order: ``session_reports.created_at DESC`` matches
        ``SessionReportsRepository.list_for_project`` so the trends
        endpoint's session set is consistent with the
        ``/api/analysis/sessions`` list.

        Corrupt enum rows: if a behavior_flags row's ``flag_type`` is
        outside ``BehaviorFlagType`` (only possible if a manual SQL
        edit corrupted the table), it is logged and skipped — never
        silently folded into a sibling enum value. Mirrors
        ``count_by_type``'s precedent.

        Returns:
            list[SessionFlagBreakdown] — at most `limit` entries,
            ordered by analyzed_at DESC. Empty list if no analyzed
            sessions in the project.
        """
        # Subquery: most-recent <limit> analyzed sessions for this project.
        # The LIMIT lives ON THIS subquery — closing DC-7.
        recent = (
            sa.select(
                session_reports.c.session_id,
                session_reports.c.created_at.label("analyzed_at"),
            )
            .where(session_reports.c.project_id == project_id)
            .order_by(
                session_reports.c.created_at.desc(),
                session_reports.c.session_id.asc(),
            )
            .limit(limit)
            .subquery()
        )

        # LEFT JOIN flags by (session_id, project_id). The project_id
        # equality on both sides defends against cross-project leak even
        # if a downstream caller bypasses the api-layer DC-1 check.
        stmt = (
            sa.select(
                recent.c.session_id,
                recent.c.analyzed_at,
                behavior_flags.c.flag_type,
                sa.func.count(behavior_flags.c.id).label("cnt"),
            )
            .select_from(
                recent.outerjoin(
                    behavior_flags,
                    sa.and_(
                        behavior_flags.c.session_id == recent.c.session_id,
                        behavior_flags.c.project_id == project_id,
                    ),
                )
            )
            .group_by(
                recent.c.session_id,
                recent.c.analyzed_at,
                behavior_flags.c.flag_type,
            )
            .order_by(
                recent.c.analyzed_at.desc(),
                recent.c.session_id.asc(),
            )
        )

        # Build per-session dicts in Python. The session set is bounded
        # by `limit`, and each session has at most |BehaviorFlagType|
        # rows in the result, so memory is bounded too.
        per_session: dict[str, SessionFlagBreakdown] = {}
        order: list[str] = []
        with self._db.engine.connect() as conn:
            for row in conn.execute(stmt):
                session_id, analyzed_at, flag_type_raw, cnt = row
                if session_id not in per_session:
                    per_session[session_id] = SessionFlagBreakdown(
                        session_id=session_id,
                        analyzed_at=analyzed_at,
                        counts_by_type={},
                    )
                    order.append(session_id)
                if flag_type_raw is None:
                    # Zero-flag session — LEFT JOIN produced a single row
                    # with NULL flag_type and cnt=0 (in SQLite, COUNT(NULL)=0).
                    continue
                try:
                    ft = BehaviorFlagType(flag_type_raw)
                except ValueError:
                    _logger.warning(
                        "behavior_flags.flag_type=%r outside enum; "
                        "skipping in count_per_session_for_project for "
                        "project_id=%r session_id=%r",
                        flag_type_raw,
                        project_id,
                        session_id,
                    )
                    continue
                per_session[session_id].counts_by_type[ft] = int(cnt)

        return [per_session[sid] for sid in order]

    @staticmethod
    def _guard(flag: BehaviorFlag) -> None:
        """Defensive re-validation against model_construct() bypass.

        Pydantic's model_construct() is the project-wide perf escape
        hatch: it skips ALL field validators. Without this guard, a
        flag with `flag_type='bogus_type'` would silently round-trip.
        """
        # Confidence first — Literal[...] is harder to detect after
        # model_construct because Pydantic v2 doesn't enforce Literals
        # on raw assignments.
        if flag.confidence not in VALID_CONFIDENCE:
            raise ValueError(
                f"BehaviorFlag.confidence={flag.confidence!r} must be "
                f"one of {sorted(VALID_CONFIDENCE)}"
            )

        # flag_type may be a BehaviorFlagType instance OR a raw string
        # depending on how the flag was constructed. Normalize via the
        # enum constructor — it raises ValueError for unknown values.
        if isinstance(flag.flag_type, BehaviorFlagType):
            return
        try:
            BehaviorFlagType(flag.flag_type)
        except ValueError as e:
            raise ValueError(
                f"BehaviorFlag.flag_type={flag.flag_type!r} is not a "
                f"valid BehaviorFlagType"
            ) from e

    @staticmethod
    def _flag_to_row(flag: BehaviorFlag) -> dict[str, Any]:
        flag_type_value = (
            flag.flag_type.value
            if isinstance(flag.flag_type, BehaviorFlagType)
            else BehaviorFlagType(flag.flag_type).value
        )
        return {
            "id": flag.id,
            "project_id": flag.project_id,
            "session_id": flag.session_id,
            "segment_index": flag.segment_index,
            "flag_type": flag_type_value,
            "event_ids": json.dumps(flag.event_ids, ensure_ascii=False),
            "intent_summary": flag.intent_summary,
            "reason": flag.reason,
            "confidence": flag.confidence,
            "created_at": flag.created_at,
        }

    @staticmethod
    def _row_to_flag(row: sa.RowMapping) -> BehaviorFlag:
        return BehaviorFlag(
            id=row["id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            segment_index=row["segment_index"],
            flag_type=BehaviorFlagType(row["flag_type"]),
            event_ids=json.loads(row["event_ids"]),
            intent_summary=row["intent_summary"],
            reason=row["reason"],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )
