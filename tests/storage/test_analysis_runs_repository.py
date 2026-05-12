"""Death + lifecycle + happy-path tests for AnalysisRunsRepository (GUR-102 task-1).

Death cases:
- DT-1.1.a: start_run inserts the row BEFORE any pipeline work; the row
  exists at stage='pending' even when code immediately after raises.
- DT-1.1.b: advance_stage rejects invalid stage enum at repo layer
  (mirrors GUR-100 D1 pattern on behavior_flags.flag_type).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from secondsight.analysis.schemas import AnalysisRunStage
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.db_engine import DBEngine


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[AnalysisRunsRepository]:
    eng = DBEngine(tmp_path / "intel.db")
    r = AnalysisRunsRepository(eng)
    r.create_schema()
    try:
        yield r
    finally:
        eng.dispose()


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_1_1_a_start_run_row_exists_before_pipeline_work(
        self, repo: AnalysisRunsRepository
    ) -> None:
        """DT-1.1.a — start_run inserts at stage='pending' BEFORE any
        pipeline work begins. Simulates SIGKILL after the insert by
        calling start_run, then immediately raising; asserts the row
        is still in the DB (audit trail for retry logic).

        Death case: if start_run inserts at stage EXIT rather than
        entry, a SIGKILL after pipeline work but before the insert
        leaves zero audit trail. The retry logic would re-analyze
        an already-partially-analyzed session, duplicating flags.
        """
        run_id = repo.start_run(project_id="proj-1", session_id="sess-1")

        # Simulate immediate crash after start_run.
        try:
            raise RuntimeError("SIGKILL simulation")
        except RuntimeError:
            pass  # crash, but row should already be committed

        # Row must be visible to a query — the 'pending' audit trail
        row = repo.get_latest_for_session("sess-1")
        assert row is not None, (
            "start_run must commit the row at stage entry — "
            "no audit trail for retry logic after crash"
        )
        assert row.id == run_id
        assert row.stage == AnalysisRunStage.PENDING
        assert row.completed_at is None

    def test_dt_1_1_b_advance_stage_rejects_invalid_stage_enum(
        self, repo: AnalysisRunsRepository
    ) -> None:
        """DT-1.1.b — advance_stage validates the stage enum at the
        repository layer (no DB CHECK constraint). A 'bogus_stage'
        string must raise ValueError before any DB write.

        Death case: if the enum guard is absent, a typo or refactor
        that renames a stage constant silently writes a bad string to
        the DB. The audit query (count_recent_partial) would then
        silently exclude these rows since the stage doesn't match
        its terminal-stages filter.
        """
        run_id = repo.start_run(project_id="proj-1", session_id="sess-1")

        with pytest.raises(ValueError) as exc:
            repo.advance_stage(run_id, "bogus_stage")

        assert "bogus_stage" in str(exc.value) or "stage" in str(exc.value).lower()

        # The row must still be at 'pending' — no partial write.
        row = repo.get_latest_for_session("sess-1")
        assert row is not None
        assert row.stage == AnalysisRunStage.PENDING, (
            "advance_stage must not write to DB before enum validation"
        )

    def test_dt_1_2_advance_stage_unknown_run_id_raises(self, repo: AnalysisRunsRepository) -> None:
        """advance_stage on a missing run_id must raise, not silently no-op.

        Silent no-op here would mean a pipeline transition is "confirmed"
        but the audit row never advanced — a particularly dangerous silent
        failure because the orchestrator might proceed to the next stage.
        """
        with pytest.raises(LookupError):
            repo.advance_stage("does-not-exist", "segmented")

    def test_dt_1_3_record_failure_unknown_run_id_raises(
        self, repo: AnalysisRunsRepository
    ) -> None:
        """record_failure on a missing run_id must raise, not silently no-op."""
        with pytest.raises(LookupError):
            repo.record_failure("does-not-exist", "pipeline crashed")


# =====================================================================
# HAPPY PATHS / LIFECYCLE
# =====================================================================


class TestLifecycle:
    def test_full_pipeline_pending_to_aggregated(self, repo: AnalysisRunsRepository) -> None:
        """Full lifecycle: pending → segmented → behavior_done →
        summary_written → aggregated.

        completed_at must be None for all non-terminal stages; populated
        on the terminal 'aggregated' stage.
        """
        run_id = repo.start_run(project_id="proj-1", session_id="sess-1")

        # pending — no completed_at
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.stage == AnalysisRunStage.PENDING
        assert run.completed_at is None

        # segmented
        repo.advance_stage(run_id, "segmented")
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.stage == AnalysisRunStage.SEGMENTED
        assert run.completed_at is None

        # behavior_done (flags_inserted recorded here)
        repo.advance_stage(run_id, "behavior_done", flags_inserted=7)
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.stage == AnalysisRunStage.BEHAVIOR_DONE
        assert run.flags_inserted == 7
        assert run.completed_at is None

        # summary_written — terminal, must set completed_at
        repo.advance_stage(run_id, "summary_written")
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.stage == AnalysisRunStage.SUMMARY_WRITTEN
        assert run.completed_at is not None, (
            "completed_at must be populated on terminal stage 'summary_written'"
        )

        # aggregated — terminal, must set completed_at
        repo.advance_stage(run_id, "aggregated")
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.stage == AnalysisRunStage.AGGREGATED
        assert run.completed_at is not None, (
            "completed_at must be populated on terminal stage 'aggregated'"
        )

    def test_record_failure_sets_failed_and_completed_at(
        self, repo: AnalysisRunsRepository
    ) -> None:
        run_id = repo.start_run(project_id="proj-1", session_id="sess-1")
        repo.record_failure(run_id, "LLM timeout")

        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.stage == AnalysisRunStage.FAILED
        assert run.error_message == "LLM timeout"
        assert run.completed_at is not None, "record_failure must set completed_at"

    def test_flags_inserted_default_zero(self, repo: AnalysisRunsRepository) -> None:
        """flags_inserted defaults to 0; only non-zero after behavior_done."""
        run_id = repo.start_run(project_id="proj-1", session_id="sess-1")
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.flags_inserted == 0

        repo.advance_stage(run_id, "segmented")
        run = repo.get_latest_for_session("sess-1")
        assert run is not None
        assert run.flags_inserted == 0  # not affected by non-behavior_done

    def test_get_latest_for_session_returns_most_recent_by_started_at(
        self, repo: AnalysisRunsRepository
    ) -> None:
        """When multiple runs exist for same session, latest by started_at wins."""
        run_id1 = repo.start_run(project_id="proj-1", session_id="sess-x")
        run_id2 = repo.start_run(project_id="proj-1", session_id="sess-x")

        # Fail the first run
        repo.record_failure(run_id1, "error")

        # get_latest must return the second (most recent) run
        latest = repo.get_latest_for_session("sess-x")
        assert latest is not None
        assert latest.id == run_id2

    def test_get_latest_for_session_returns_none_when_missing(
        self, repo: AnalysisRunsRepository
    ) -> None:
        result = repo.get_latest_for_session("no-such-session")
        assert result is None

    def test_count_recent_partial_counts_non_terminal_rows(
        self, repo: AnalysisRunsRepository
    ) -> None:
        """count_recent_partial: rows NOT in (summary_written, aggregated,
        failed) AND updated_at < since. Used by ship-manifest review.
        """
        # Create a partial run (pending — non-terminal)
        run_id = repo.start_run(project_id="proj-1", session_id="sess-1")

        # Create a completed run
        run_id2 = repo.start_run(project_id="proj-1", session_id="sess-2")
        repo.advance_stage(run_id2, "aggregated")

        # Since far future: both runs are "old enough" to count
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        count = repo.count_recent_partial(since=far_future)
        assert count == 1, (
            f"Expected 1 partial run, got {count}. "
            "Only non-terminal rows (not summary_written/aggregated/failed) "
            "with updated_at < since should be counted."
        )

    def test_create_schema_idempotent(self, tmp_path: Path) -> None:
        eng = DBEngine(tmp_path / "intel.db")
        try:
            r = AnalysisRunsRepository(eng)
            r.create_schema()
            r.create_schema()  # second call must not raise
        finally:
            eng.dispose()

    def test_get_latest_for_session_id_desc_tiebreak(self, repo: AnalysisRunsRepository) -> None:
        """IMPORTANT-4: When two runs share the same started_at (microsecond
        identical), the tiebreak on `id DESC` must be deterministic.

        Without the tiebreak, the ordering is DB-implementation-defined and
        can return either run non-deterministically. This would cause retry
        logic to unpredictably resume the wrong run.
        """
        from secondsight.storage.analysis_runs_table import analysis_runs

        # Insert two rows with identical started_at via direct SQL
        # (start_run() uses datetime.now() which would differ by microseconds)
        identical_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        rows = [
            {
                "id": "run-tiebreak-aaa",
                "project_id": "proj-1",
                "session_id": "sess-tie",
                "stage": "pending",
                "started_at": identical_ts,
                "updated_at": identical_ts,
                "completed_at": None,
                "error_message": None,
                "flags_inserted": 0,
            },
            {
                "id": "run-tiebreak-zzz",
                "project_id": "proj-1",
                "session_id": "sess-tie",
                "stage": "pending",
                "started_at": identical_ts,  # same timestamp
                "updated_at": identical_ts,
                "completed_at": None,
                "error_message": None,
                "flags_inserted": 0,
            },
        ]
        with repo._db.engine.begin() as conn:
            conn.execute(analysis_runs.insert(), rows)

        # id DESC: "run-tiebreak-zzz" > "run-tiebreak-aaa" lexicographically
        latest = repo.get_latest_for_session("sess-tie")
        assert latest is not None
        assert latest.id == "run-tiebreak-zzz", (
            "Tiebreak on id DESC must return lexicographically larger id "
            "when started_at values are identical"
        )


# =====================================================================
# CORRUPT DB STAGE — DEATH TEST (CRITICAL-3 fix)
# =====================================================================


class TestCorruptDBStage:
    def test_dt_corrupt_stage_raises_value_error_with_context(
        self, repo: AnalysisRunsRepository
    ) -> None:
        """CRITICAL-3: _row_to_run must raise ValueError with context when
        the DB contains a stage string not in AnalysisRunStage.

        Death case: before the fix, AnalysisRunStage(row["stage"]) raised a
        bare ValueError with only 'bogus_stage' is not a valid AnalysisRunStage.
        The caller (get_latest_for_session) only documented LookupError, so
        the ValueError propagated undocumented to the orchestrator. The run_id
        and session_id needed for manual DB repair were not in the message.

        After the fix, the ValueError includes run_id, session_id, and the
        bad stage value.
        """
        from secondsight.storage.analysis_runs_table import analysis_runs

        # Insert a row with a corrupt stage value directly via SQL
        corrupt_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        with repo._db.engine.begin() as conn:
            conn.execute(
                analysis_runs.insert().values(
                    id="run-corrupt-stage",
                    project_id="proj-1",
                    session_id="sess-corrupt",
                    stage="INVALID_STAGE_NOT_IN_ENUM",
                    started_at=corrupt_ts,
                    updated_at=corrupt_ts,
                    completed_at=None,
                    error_message=None,
                    flags_inserted=0,
                )
            )

        with pytest.raises(ValueError) as exc:
            repo.get_latest_for_session("sess-corrupt")

        msg = str(exc.value)
        # Must include the run_id and session_id for manual repair
        assert "run-corrupt-stage" in msg, "ValueError must include run_id to aid manual DB repair"
        assert "sess-corrupt" in msg, "ValueError must include session_id to aid manual DB repair"
        assert "INVALID_STAGE_NOT_IN_ENUM" in msg, "ValueError must include the bad stage value"
