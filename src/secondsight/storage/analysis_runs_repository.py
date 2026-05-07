"""AnalysisRunsRepository — SQLAlchemy Core repository (GUR-102 task-1).

Audit contract:
    start_run() inserts the row at stage='pending' BEFORE any pipeline
    work begins. A SIGKILL after the insert leaves a 'pending' row as
    the audit trail for retry logic (DC-1). Stage transitions are
    explicit advance_stage() calls — never implicit.

Defensive enum guard (D1):
    advance_stage() validates the stage string against AnalysisRunStage
    at the repository layer (no DB CHECK). Mirrors GUR-100's
    behavior_flags.flag_type guard. Without this guard, a refactor that
    renames a stage constant silently writes a bad string; the
    count_recent_partial audit query would then silently exclude those
    rows since the stage doesn't match its terminal-stages filter.

Terminal stage semantics:
    'aggregated' and 'failed' set completed_at = now().
    'summary_written' also sets completed_at (pipeline artifact done).
    Other transitions leave completed_at NULL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from secondsight.analysis.schemas import AnalysisRun, AnalysisRunStage, TERMINAL_STAGES
from secondsight.storage.analysis_runs_table import analysis_runs, metadata
from secondsight.storage.db_engine import DBEngine

_VALID_STAGES: frozenset[str] = frozenset(s.value for s in AnalysisRunStage)

# Single source of truth lives in schemas.py co-located with AnalysisRunStage.
# When adding a new terminal stage: update TERMINAL_STAGES in schemas.py only.
_NON_TERMINAL_STAGES: frozenset[str] = _VALID_STAGES - TERMINAL_STAGES

_logger = logging.getLogger(__name__)


def _new_id() -> str:
    """Generate a unique run ID using UUID4."""
    import uuid

    return f"run-{uuid.uuid4()}"


class AnalysisRunsRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create analysis_runs + indexes if absent. Idempotent."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def start_run(self, project_id: str, session_id: str) -> str:
        """Insert a new row at stage='pending'. Returns the run ID.

        IMPORTANT: this is called BEFORE any pipeline work begins —
        if the process dies after this insert, the row is the audit
        trail. Stage transitions are explicit advance_stage() calls.

        The insert uses engine.begin() so the row is committed before
        returning. Do NOT defer this call; calling it "when convenient"
        breaks DC-1.
        """
        run_id = _new_id()
        now = datetime.now(timezone.utc)
        row: dict[str, Any] = {
            "id": run_id,
            "project_id": project_id,
            "session_id": session_id,
            "stage": AnalysisRunStage.PENDING.value,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "error_message": None,
            "flags_inserted": 0,
        }
        with self._db.engine.begin() as conn:
            conn.execute(analysis_runs.insert().values(**row))
        return run_id

    def advance_stage(
        self, run_id: str, stage: str, *, flags_inserted: int = 0
    ) -> None:
        """Move a run to the next stage.

        Validates stage against the AnalysisRunStage enum at the
        repository layer (no DB CHECK). flags_inserted is recorded
        on the 'behavior_done' transition; ignored on others.

        Raises:
            ValueError — stage is not a valid AnalysisRunStage value.
            LookupError — run_id not found (never silently no-ops).
        """
        if stage not in _VALID_STAGES:
            raise ValueError(
                f"advance_stage: stage={stage!r} is not a valid "
                f"AnalysisRunStage. Valid values: {sorted(_VALID_STAGES)}"
            )

        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "stage": stage,
            "updated_at": now,
        }

        if stage in TERMINAL_STAGES:
            values["completed_at"] = now

        if stage == AnalysisRunStage.BEHAVIOR_DONE.value:
            values["flags_inserted"] = flags_inserted

        stmt = (
            sa.update(analysis_runs)
            .where(analysis_runs.c.id == run_id)
            .values(**values)
        )
        with self._db.engine.begin() as conn:
            result = conn.execute(stmt)
            if result.rowcount == 0:
                raise LookupError(
                    f"analysis_run {run_id!r} not found; "
                    "advance_stage will not silently no-op"
                )

    def record_failure(self, run_id: str, error_message: str) -> None:
        """Mark a run as failed with completed_at = now().

        Raises:
            LookupError — run_id not found.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            sa.update(analysis_runs)
            .where(analysis_runs.c.id == run_id)
            .values(
                stage=AnalysisRunStage.FAILED.value,
                error_message=error_message,
                completed_at=now,
                updated_at=now,
            )
        )
        with self._db.engine.begin() as conn:
            result = conn.execute(stmt)
            if result.rowcount == 0:
                raise LookupError(
                    f"analysis_run {run_id!r} not found; "
                    "record_failure will not silently no-op"
                )

    def get_latest_for_session(self, session_id: str) -> AnalysisRun | None:
        """Latest run by started_at DESC, with id DESC as tiebreak.

        Used to decide resume vs. new run vs. SessionAlreadyAnalyzedError.
        Returns None if no run exists for this session_id.

        Raises:
            ValueError — DB row contains a stage value not in AnalysisRunStage
                (corrupt data). Includes run_id, session_id, and the bad stage
                value in the message to aid manual repair.
        """
        stmt = (
            sa.select(analysis_runs)
            .where(analysis_runs.c.session_id == session_id)
            .order_by(
                analysis_runs.c.started_at.desc(),
                analysis_runs.c.id.desc(),  # tiebreak: deterministic on same-microsecond inserts
            )
            .limit(1)
        )
        with self._db.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_run(row) if row else None

    def count_recent_partial(self, since: datetime) -> int:
        """Audit query: rows where stage is non-terminal AND updated_at < since.

        Non-terminal stages: pending, segmented, behavior_done.
        Used by ship-manifest review to surface stuck pipeline runs.

        Args:
            since: datetime threshold. Rows with updated_at < since are
                   counted (i.e., runs that have been stuck since before
                   this timestamp).
        """
        stmt = (
            sa.select(sa.func.count())
            .select_from(analysis_runs)
            .where(
                sa.and_(
                    analysis_runs.c.stage.in_(list(_NON_TERMINAL_STAGES)),
                    analysis_runs.c.updated_at < since,
                )
            )
        )
        with self._db.engine.connect() as conn:
            result = conn.execute(stmt).scalar()
            return int(result) if result is not None else 0

    @staticmethod
    def _row_to_run(row: sa.RowMapping) -> AnalysisRun:
        raw_stage = row["stage"]
        try:
            stage = AnalysisRunStage(raw_stage)
        except ValueError as exc:
            raise ValueError(
                f"_row_to_run: corrupt stage value in DB row — "
                f"run_id={row['id']!r}, session_id={row['session_id']!r}, "
                f"stage={raw_stage!r} is not a valid AnalysisRunStage. "
                f"Valid values: {[s.value for s in AnalysisRunStage]}. "
                f"Manual DB repair required."
            ) from exc
        return AnalysisRun(
            id=row["id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            stage=stage,
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            error_message=row["error_message"],
            flags_inserted=row["flags_inserted"],
        )
