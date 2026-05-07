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
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.analysis.schemas import VALID_CONFIDENCE, BehaviorFlag, BehaviorFlagType
from secondsight.storage.behavior_flags_table import behavior_flags, metadata
from secondsight.storage.db_engine import DBEngine

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
