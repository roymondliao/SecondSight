"""AnalysisOutputsRepository — persists AnalysisOutput from ModeAwareDispatch (Task 6).

Stores one row per dispatch attempt keyed on session_id (UNIQUE).
The UNIQUE constraint on session_id is the DB-level DC10 deduplication guard:
if two concurrent dispatches both complete, only one row is kept.

INSERT OR IGNORE semantics: the second concurrent write is silently dropped
rather than raising IntegrityError. The application-level asyncio.Lock in
ModeAwareDispatch is the primary DC10 guard; the DB constraint is defense-in-depth.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from secondsight.analysis.output import AnalysisOutput
from secondsight.storage.analysis_outputs_table import analysis_outputs, metadata
from secondsight.storage.db_engine import DBEngine


class AnalysisOutputsRepository:
    """Persists AnalysisOutput rows from ModeAwareDispatch.dispatch()."""

    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create analysis_outputs table if absent. Idempotent (checkfirst=True)."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def insert_or_ignore(self, output: AnalysisOutput, *, project_id: str) -> str:
        """Persist an AnalysisOutput row.

        Uses INSERT OR IGNORE semantics: if a row already exists for this
        session_id (due to a concurrent dispatch race that escaped the
        asyncio.Lock), the insert is silently ignored and the existing
        row_id is returned.

        Args:
            output: The AnalysisOutput to persist.
            project_id: The project this session belongs to.

        Returns:
            The row ID (a new UUID if inserted, or the existing ID if ignored).
        """
        row_id = f"ao-{uuid.uuid4()}"
        now = datetime.now(timezone.utc)

        error_details_json: str | None = None
        if output.error_details is not None:
            try:
                error_details_json = json.dumps(output.error_details)
            except (TypeError, ValueError):
                error_details_json = json.dumps({"raw": str(output.error_details)})

        row = {
            "id": row_id,
            "session_id": output.session_id,
            "project_id": project_id,
            "dispatched_via": output.dispatched_via,
            "cli_agent": output.cli_agent,
            "primary_model": output.primary_model,
            "fallback_used": 1 if output.fallback_used else 0,
            "retry_count": output.retry_count,
            "status": output.status,
            "error_details": error_details_json,
            "created_at": now,
        }

        # INSERT OR IGNORE: second concurrent write is dropped (DC10 defense-in-depth).
        stmt = analysis_outputs.insert().prefix_with("OR IGNORE").values(**row)
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

        return row_id

    def get_by_session_id(self, session_id: str) -> dict | None:
        """Fetch the analysis output row for a session, if any.

        Returns:
            A dict with all output fields, or None if no row exists.
            error_details is returned as a dict (deserialized from JSON), or None.
        """
        stmt = (
            sa.select(analysis_outputs)
            .where(analysis_outputs.c.session_id == session_id)
            .limit(1)
        )
        with self._db.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if row is None:
            return None

        error_details = None
        if row["error_details"] is not None:
            try:
                error_details = json.loads(row["error_details"])
            except (json.JSONDecodeError, TypeError):
                error_details = {"raw": row["error_details"]}

        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "project_id": row["project_id"],
            "dispatched_via": row["dispatched_via"],
            "cli_agent": row["cli_agent"],
            "primary_model": row["primary_model"],
            "fallback_used": bool(row["fallback_used"]),
            "retry_count": row["retry_count"],
            "status": row["status"],
            "error_details": error_details,
            "created_at": row["created_at"],
        }


__all__ = ["AnalysisOutputsRepository"]
