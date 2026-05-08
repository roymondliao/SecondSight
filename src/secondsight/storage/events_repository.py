"""EventsRepository — SQLAlchemy Core repository over `events` table (P1-3).

Idempotency contract:
    insert(event) is idempotent on `id`. Two calls with the same id and
    different data produce one row — the FIRST one. Use `INSERT … ON
    CONFLICT(id) DO NOTHING`.

    BUT: a UNIQUE(session_id, sequence_number) violation MUST raise.
    Same sequence_number with different id is a correctness bug, never
    a retry. We do not silence it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.event import Event, EventType
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_table import events, metadata


class EventsRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create the table + indexes if absent. Idempotent."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def insert(self, event: Event) -> None:
        """Insert one event. Idempotent on `id`.

        Raises:
            sqlalchemy.exc.IntegrityError: if (session_id, sequence_number)
                conflict — this is an upstream correctness bug, not a retry.
        """
        row = self._event_to_row(event)
        stmt = (
            sqlite_insert(events)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

    def insert_many(self, batch: Sequence[Event]) -> int:
        """Insert many events. Returns count attempted (not necessarily
        equal to rows inserted, since ON CONFLICT may skip duplicates).
        """
        if not batch:
            return 0
        rows = [self._event_to_row(e) for e in batch]
        stmt = sqlite_insert(events).on_conflict_do_nothing(index_elements=["id"])
        with self._db.engine.begin() as conn:
            conn.execute(stmt, rows)
        return len(rows)

    def get_session_events(self, session_id: str) -> list[Event]:
        stmt = (
            sa.select(events)
            .where(events.c.session_id == session_id)
            .order_by(events.c.sequence_number.asc())
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_event(row) for row in conn.execute(stmt).mappings()]

    def get_segment_events(self, session_id: str, segment_index: int) -> list[Event]:
        stmt = (
            sa.select(events)
            .where(
                sa.and_(
                    events.c.session_id == session_id,
                    events.c.segment_index == segment_index,
                )
            )
            .order_by(events.c.sequence_number.asc())
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_event(row) for row in conn.execute(stmt).mappings()]

    def get_max_segment_index(self, session_id: str) -> int | None:
        """Returns None if the session has no events.
        Returns int (possibly 0) otherwise.
        """
        stmt = sa.select(sa.func.max(events.c.segment_index)).where(
            events.c.session_id == session_id
        )
        with self._db.engine.connect() as conn:
            value = conn.execute(stmt).scalar()
        return int(value) if value is not None else None

    def exists(self, event_id: str) -> bool:
        stmt = sa.select(events.c.id).where(events.c.id == event_id).limit(1)
        with self._db.engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def find_stale_session_candidates(
        self,
        *,
        project_id: str | None,
        last_event_before: datetime,
    ) -> list[tuple[str, str, datetime]]:
        """Return (project_id, session_id, last_event_ts) for sessions whose
        most recent event is older than ``last_event_before``. Optional project filter.

        Used by Sweeper to find timeout-fallback candidates. Caller filters
        by analysis_runs stage separately (orchestration concern).

        This is a public method so Sweeper does not need to reach into the
        private ``_db.engine`` attribute (avoids private-API coupling that
        would break silently if EventsRepository changes its internal structure).

        Args:
            project_id: If provided, only return sessions for this project.
                If None, return sessions across ALL projects.
            last_event_before: Only sessions whose max(timestamp) < this value
                are returned. Must be timezone-aware UTC or timezone-naive
                (SQLite stores naive datetimes; naive comparison is correct).

        Returns:
            List of (project_id, session_id, last_event_ts) tuples.
            last_event_ts is the max(timestamp) value for that session.
            Empty list if no sessions qualify.
        """
        stmt = (
            sa.select(
                events.c.project_id,
                events.c.session_id,
                sa.func.max(events.c.timestamp).label("last_event_ts"),
            )
            .group_by(events.c.project_id, events.c.session_id)
            .having(sa.func.max(events.c.timestamp) < last_event_before)
        )
        if project_id is not None:
            stmt = stmt.where(events.c.project_id == project_id)

        with self._db.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [
            (row["project_id"], row["session_id"], row["last_event_ts"])
            for row in rows
        ]

    def get_latest_session_agent_type(self, project_id: str) -> str | None:
        """Return the agent_type for the most-recent session for this project.

        SCHEMA NOTE: The current `events` table (Phase 1) does not have an
        `agent_type` column (see events_table.py). This method always returns
        None until the schema is extended in a future migration.

        When it returns None, select_model()'s 'auto' mode raises
        ModelSelectionError with a `suggested_config` TOML snippet that points
        the operator to set an explicit `default_agent`. It does NOT silently
        fall back to "claude_code" — auto-mode is a two-condition feature
        (user opt-in AND events present); without either, failure is loud
        rather than a silent provider swap.

        Future implementation (when agent_type column is added):
            stmt = (
                sa.select(events.c.agent_type)
                .where(events.c.project_id == project_id)
                .order_by(events.c.timestamp.desc())
                .limit(1)
            )
            with self._db.engine.connect() as conn:
                row = conn.execute(stmt).first()
            return row[0] if row else None

        Returns:
            None (always, until schema migration adds agent_type column).
        """
        return None

    @staticmethod
    def _event_to_row(event: Event) -> dict[str, Any]:
        return {
            "id": event.id,
            "session_id": event.session_id,
            "project_id": event.project_id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp,
            "sequence_number": event.sequence_number,
            "segment_index": event.segment_index,
            "sub_agent_id": event.sub_agent_id,
            "depth": event.depth,
            "duration_ms": event.duration_ms,
            "token_count": event.token_count,
            "data": json.dumps(event.data, ensure_ascii=False),
        }

    @staticmethod
    def _row_to_event(row: sa.RowMapping) -> Event:
        return Event(
            id=row["id"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            event_type=EventType(row["event_type"]),
            timestamp=row["timestamp"],
            sequence_number=row["sequence_number"],
            segment_index=row["segment_index"],
            sub_agent_id=row["sub_agent_id"],
            depth=row["depth"],
            duration_ms=row["duration_ms"],
            token_count=row["token_count"],
            data=json.loads(row["data"]),
        )
