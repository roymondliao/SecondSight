"""AnalysisOutputsRepository — persists AnalysisOutput from ModeAwareDispatch.

Stores one latest-result row per session_id. The UNIQUE constraint on
session_id remains the DB-level DC10 deduplication guard: concurrent writes
still collapse to one row, while a later sequential re-run updates that row
with the newest dispatch result.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.storage.analysis_outputs_table import analysis_outputs, metadata
from secondsight.storage.db_engine import DBEngine

if TYPE_CHECKING:
    from secondsight.analysis.output import AnalysisOutput


class AnalysisOutputsRepository:
    """Persists AnalysisOutput rows from ModeAwareDispatch.dispatch()."""

    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create analysis_outputs table if absent. Idempotent (checkfirst=True)."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def upsert(self, output: AnalysisOutput, *, project_id: str) -> str:
        """Persist an AnalysisOutput row.

        Uses UPSERT semantics on ``session_id``: the first dispatch inserts a
        row; a later sequential re-run updates the existing row in place so the
        table continues to represent the latest mode-aware dispatch result for
        that session.

        Args:
            output: The AnalysisOutput to persist.
            project_id: The project this session belongs to.

        Returns:
            The row ID written for this dispatch.
        """
        row_id = f"ao-{uuid.uuid4()}"
        now = datetime.now(timezone.utc)

        error_details_json: str | None = None
        if output.error_details is not None:
            try:
                error_details_json = json.dumps(output.error_details)
            except TypeError, ValueError:
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

        stmt = (
            sqlite_insert(analysis_outputs)
            .values(**row)
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "dispatched_via": row["dispatched_via"],
                    "cli_agent": row["cli_agent"],
                    "primary_model": row["primary_model"],
                    "fallback_used": row["fallback_used"],
                    "retry_count": row["retry_count"],
                    "status": row["status"],
                    "error_details": row["error_details"],
                    "created_at": row["created_at"],
                },
            )
        )
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
            sa.select(analysis_outputs).where(analysis_outputs.c.session_id == session_id).limit(1)
        )
        with self._db.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if row is None:
            return None

        error_details = None
        if row["error_details"] is not None:
            try:
                error_details = json.loads(row["error_details"])
            except json.JSONDecodeError, TypeError:
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
