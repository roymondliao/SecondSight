"""SessionReportsRepository — SQLAlchemy Core repository (GUR-102 task-1).

Idempotency contract:
    upsert(report) is idempotent on `session_id`. ON CONFLICT(session_id)
    DO UPDATE updates mutable fields (analysis_run_id, headline,
    key_findings, body, updated_at). created_at is preserved from the
    original insert (never overwritten).

JSON column:
    `key_findings` is stored as JSON-encoded list[str]. Order is
    preserved on both encode and decode.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.analysis.schemas import SessionReport
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.session_reports_table import metadata, session_reports

_logger = logging.getLogger(__name__)


class SessionReportsRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create session_reports + indexes if absent. Idempotent."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def upsert(self, report: SessionReport) -> None:
        """ON CONFLICT(session_id) DO UPDATE SET
           analysis_run_id, headline, key_findings, body, updated_at.
           created_at is preserved from the FIRST insert.

        This enables re-runs to update the session artifact without
        accumulating duplicate rows. The UNIQUE(session_id) constraint
        guarantees at most one report per session.

        Raises:
            ValueError — defensive guard rejects constraint violations that
                model_construct() would bypass (key_findings > 5 items, or
                headline length out of bounds).
        """
        self._guard(report)
        row = self._report_to_row(report)
        stmt = (
            sqlite_insert(session_reports)
            .values(**row)
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    "analysis_run_id": row["analysis_run_id"],
                    "headline": row["headline"],
                    "key_findings": row["key_findings"],
                    "body": row["body"],
                    "updated_at": row["updated_at"],
                    # created_at intentionally NOT updated — preserved on conflict
                },
            )
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

    def get_for_session(self, session_id: str) -> SessionReport | None:
        """Return the report for this session, or None if absent."""
        stmt = sa.select(session_reports).where(session_reports.c.session_id == session_id)
        with self._db.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_report(row) if row else None

    def list_for_project(
        self,
        project_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionReport]:
        """Return reports for this project ordered by created_at DESC."""
        stmt = (
            sa.select(session_reports)
            .where(session_reports.c.project_id == project_id)
            .order_by(session_reports.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_report(r) for r in conn.execute(stmt).mappings()]

    @staticmethod
    def _guard(report: SessionReport) -> None:
        """Defensive re-validation against model_construct() bypass.

        model_construct() skips Pydantic validators, so constraints defined
        as Field(max_length=...) or Field(min_length=...) are not enforced.
        This guard re-checks them explicitly before any DB write.

        Validates:
        1. key_findings has at most 5 items (matches prompts/summary.py:43).
        2. headline is non-empty (min_length=1).
        3. headline is at most 200 characters (max_length=200).
        """
        if len(report.key_findings) > 5:
            raise ValueError(
                f"SessionReport.key_findings has {len(report.key_findings)} items; "
                f"max is 5 (matches prompts/summary.py:43 constraint). "
                f"A model_construct() bypass may have skipped Pydantic validation."
            )
        if len(report.headline) < 1:
            raise ValueError("SessionReport.headline must not be empty (min_length=1).")
        if len(report.headline) > 200:
            raise ValueError(
                f"SessionReport.headline has {len(report.headline)} characters; "
                f"max is 200 (max_length=200)."
            )

    @staticmethod
    def _report_to_row(report: SessionReport) -> dict[str, Any]:
        return {
            "id": report.id,
            "project_id": report.project_id,
            "session_id": report.session_id,
            "analysis_run_id": report.analysis_run_id,
            "headline": report.headline,
            "key_findings": json.dumps(report.key_findings, ensure_ascii=False),
            "body": report.body,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
        }

    @staticmethod
    def _row_to_report(row: sa.RowMapping) -> SessionReport:
        return SessionReport(
            id=row["id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            analysis_run_id=row["analysis_run_id"],
            headline=row["headline"],
            key_findings=json.loads(row["key_findings"]),
            body=row["body"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
