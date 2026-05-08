"""Death tests for AnalysisResultsPurger + enumerator — task-B2 of GUR-149.

Samsara discipline: death tests first.

Death cases (from changes/2026-05-07_gur-149_analysis-ttl-and-post-analysis-trigger/2-plan.md §3):

    DC-B6: AnalysisResultsPurger partial-purge orphan guard. behavior_flags
           is deleted FIRST, session_reports SECOND. If the purger crashes
           between stages, the next run must re-detect the same session_reports
           row and complete the reap (idempotent re-attempt against an empty
           behavior_flags partition is a no-op rowcount 0). The reverse order
           would leave orphan behavior_flags with no FK to enforce cleanup.

    DC-B7: Empty install. Enumerator on an empty session_reports table must
           return [] without crashing (no min/max on empty input, no division
           by zero); purger called with [] is a clean no-op with empty
           PurgeResult.

Boundary contract (from 2-plan.md §2.2 + D1):
    - TTL boundary uses session_reports.created_at (NOT updated_at, NOT
      analysis_runs.completed_at). Re-running analysis preserves created_at
      per SessionReportsRepository.upsert (lines 5-7 of that module's
      docstring), so the boundary is stable.
    - Inclusive: a row with created_at exactly == (now - ttl) IS expired.
    - Stable order: enumerator returns ExpiredAnalysis sorted by session_id
      ascending (matches GUR-147 enumerate_expired_sessions for reproducible
      --dry-run output).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from secondsight.analysis.schemas import BehaviorFlag, BehaviorFlagType, SessionReport
from secondsight.storage.analysis_retention import (
    AnalysisResultsPurger,
    ExpiredAnalysis,
    enumerate_expired_analyses,
)
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.retention import PurgeResult
from secondsight.storage.session_reports_repository import SessionReportsRepository

UTC = timezone.utc
NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> DBEngine:
    return DBEngine(db_path=tmp_path / "intel.db")


@pytest.fixture
def reports_repo(db: DBEngine) -> SessionReportsRepository:
    repo = SessionReportsRepository(db_engine=db)
    repo.create_schema()
    return repo


@pytest.fixture
def flags_repo(db: DBEngine) -> BehaviorFlagsRepository:
    repo = BehaviorFlagsRepository(db_engine=db)
    repo.create_schema()
    return repo


@pytest.fixture
def purger(
    reports_repo: SessionReportsRepository,
    flags_repo: BehaviorFlagsRepository,
) -> AnalysisResultsPurger:
    return AnalysisResultsPurger(
        session_reports_repo=reports_repo,
        behavior_flags_repo=flags_repo,
    )


def _make_report(
    *,
    session_id: str,
    project_id: str = "proj-alpha",
    created_at: datetime,
) -> SessionReport:
    return SessionReport(
        id=f"report-{session_id}",
        project_id=project_id,
        session_id=session_id,
        analysis_run_id=f"run-{session_id}",
        headline=f"headline {session_id}",
        key_findings=["finding"],
        body=f"body {session_id}",
        created_at=created_at,
        updated_at=created_at,
    )


def _make_flag(
    *,
    session_id: str,
    project_id: str = "proj-alpha",
    flag_index: int = 0,
    created_at: datetime,
) -> BehaviorFlag:
    return BehaviorFlag(
        id=f"flag-{session_id}-{flag_index}",
        project_id=project_id,
        session_id=session_id,
        segment_index=0,
        flag_type=BehaviorFlagType.UNNECESSARY_READ,
        event_ids=[f"evt-{session_id}-0"],
        intent_summary="test intent",
        reason="test reason",
        confidence="medium",
        created_at=created_at,
    )


# ======================================================================
# DC-B7 — Empty install: no session_reports at all
# ======================================================================


class TestDCB7EmptyInstall:
    """A fresh install with no session_reports must produce a clean
    no-op enumeration and a clean no-op purge. Risk: an empty path
    that crashes (e.g., min() on an empty sequence) would brick a
    fresh deployment's first scheduled cleanup run."""

    def test_enumerator_returns_empty_list_on_empty_table(
        self, reports_repo: SessionReportsRepository
    ) -> None:
        result = enumerate_expired_analyses(
            reports_repo,
            analysis_ttl_days=30,
            now=NOW,
        )
        assert result == []

    def test_enumerator_returns_empty_list_when_no_expired_rows(
        self, reports_repo: SessionReportsRepository
    ) -> None:
        # Insert one fresh report (created 1 day ago, ttl 30 days → not expired).
        fresh = _make_report(
            session_id="sess-fresh",
            created_at=NOW - timedelta(days=1),
        )
        reports_repo.upsert(fresh)
        result = enumerate_expired_analyses(
            reports_repo,
            analysis_ttl_days=30,
            now=NOW,
        )
        assert result == []

    def test_purger_with_empty_input_returns_empty_result(
        self, purger: AnalysisResultsPurger
    ) -> None:
        result = purger.purge([])
        assert isinstance(result, PurgeResult)
        assert result.purged_session_ids == ()
        assert result.failures == ()
        assert result.had_failures is False


# ======================================================================
# DC-B6 — Partial-purge orphan guard
# ======================================================================


class TestDCB6PartialPurgeOrphanGuard:
    """The order is behavior_flags FIRST, session_reports SECOND. On
    crash between stages, the next purge call MUST re-detect the same
    session_reports row, complete the (now-empty) behavior_flags
    deletion as a no-op, and finish reaping session_reports.

    This pins the silent-failure contract: orphan behavior_flags rows
    are forbidden. There is no FK constraint enforcing this at the DB
    level; the policy is purely in the purger's order."""

    def test_purger_reaps_both_tables_for_expired_session(
        self,
        reports_repo: SessionReportsRepository,
        flags_repo: BehaviorFlagsRepository,
        purger: AnalysisResultsPurger,
    ) -> None:
        """Happy-path completeness check: both behavior_flags AND
        session_reports rows are gone after a clean purge.

        This does NOT pin the deletion order — see
        test_behavior_flags_deleted_before_session_reports for that.
        """
        old_ts = NOW - timedelta(days=400)
        report = _make_report(session_id="sess-old", created_at=old_ts)
        reports_repo.upsert(report)
        for i in range(3):
            flags_repo.insert(
                _make_flag(
                    session_id="sess-old",
                    flag_index=i,
                    created_at=old_ts,
                )
            )

        assert reports_repo.get_for_session("sess-old") is not None
        assert len(flags_repo.get_session_flags("sess-old")) == 3

        result = purger.purge([
            ExpiredAnalysis(
                session_id="sess-old",
                report_created_at=old_ts,
            )
        ])

        assert "sess-old" in result.purged_session_ids
        assert reports_repo.get_for_session("sess-old") is None
        assert flags_repo.get_session_flags("sess-old") == []

    def test_behavior_flags_deleted_before_session_reports(
        self,
        reports_repo: SessionReportsRepository,
        flags_repo: BehaviorFlagsRepository,
        purger: AnalysisResultsPurger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pin the contract: behavior_flags delete happens FIRST.

        Spy on the two private helpers to record their invocation order
        for one session. A regression that swaps the order would break
        this assertion immediately, regardless of end-state cleanup.
        """
        old_ts = NOW - timedelta(days=400)
        reports_repo.upsert(_make_report(session_id="sess-x", created_at=old_ts))
        flags_repo.insert(_make_flag(session_id="sess-x", created_at=old_ts))

        from secondsight.storage import analysis_retention as ar_module

        call_order: list[str] = []
        real_flags_delete = ar_module._delete_behavior_flags_for_session
        real_reports_delete = ar_module._delete_session_report_for_session

        def spy_flags(repo, sid):  # type: ignore[no-untyped-def]
            call_order.append(f"behavior_flags:{sid}")
            return real_flags_delete(repo, sid)

        def spy_reports(repo, sid):  # type: ignore[no-untyped-def]
            call_order.append(f"session_reports:{sid}")
            return real_reports_delete(repo, sid)

        monkeypatch.setattr(
            ar_module, "_delete_behavior_flags_for_session", spy_flags
        )
        monkeypatch.setattr(
            ar_module, "_delete_session_report_for_session", spy_reports
        )

        purger.purge([
            ExpiredAnalysis(
                session_id="sess-x",
                report_created_at=old_ts,
            )
        ])

        assert call_order == [
            "behavior_flags:sess-x",
            "session_reports:sess-x",
        ], f"order was: {call_order!r}"

    def test_partial_failure_between_stages_is_recoverable(
        self,
        reports_repo: SessionReportsRepository,
        flags_repo: BehaviorFlagsRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate: behavior_flags delete succeeds, then session_reports
        delete raises. Next purge run must complete cleanly.

        This pins DC-B6: a partial failure leaves the system in a
        forward-progressable state. Use deterministic helper-function
        monkeypatch (not engine introspection) so the spy cannot
        silently no-op on SQLAlchemy internals changes.
        """
        old_ts = NOW - timedelta(days=400)
        for sid in ("sess-a", "sess-b"):
            reports_repo.upsert(_make_report(session_id=sid, created_at=old_ts))
            flags_repo.insert(_make_flag(session_id=sid, created_at=old_ts))

        from secondsight.storage import analysis_retention as ar_module

        real_reports_delete = ar_module._delete_session_report_for_session
        raised_for_a = {"flag": False}

        def fail_first_reports_delete(repo, sid):  # type: ignore[no-untyped-def]
            if sid == "sess-a" and not raised_for_a["flag"]:
                raised_for_a["flag"] = True
                raise RuntimeError("simulated DB delete failure on sess-a")
            return real_reports_delete(repo, sid)

        monkeypatch.setattr(
            ar_module,
            "_delete_session_report_for_session",
            fail_first_reports_delete,
        )

        purger = AnalysisResultsPurger(
            session_reports_repo=reports_repo,
            behavior_flags_repo=flags_repo,
        )
        inputs = [
            ExpiredAnalysis(session_id=sid, report_created_at=old_ts)
            for sid in ("sess-a", "sess-b")
        ]
        result1 = purger.purge(inputs)

        # First pass: sess-a recorded as failure (session_reports stage),
        # sess-b succeeded fully.
        assert raised_for_a["flag"] is True, (
            "spy never fired — test would silently no-op without this guard"
        )
        assert "sess-b" in result1.purged_session_ids
        assert "sess-a" not in result1.purged_session_ids
        assert any(f.session_id == "sess-a" for f in result1.failures)
        # behavior_flags for sess-a are already deleted (first stage succeeded).
        assert flags_repo.get_session_flags("sess-a") == []
        # session_reports for sess-a remains (second stage failed).
        assert reports_repo.get_for_session("sess-a") is not None

        # Second pass: re-detect sess-a as expired, re-attempt purge.
        # behavior_flags delete is now a no-op (rowcount 0 — already gone);
        # session_reports delete succeeds this time (spy is exhausted).
        result2 = purger.purge([
            ExpiredAnalysis(session_id="sess-a", report_created_at=old_ts)
        ])
        assert "sess-a" in result2.purged_session_ids
        assert result2.failures == ()
        assert reports_repo.get_for_session("sess-a") is None
        # No orphan behavior_flags.
        assert flags_repo.get_session_flags("sess-a") == []


# ======================================================================
# Inclusive boundary + stable order
# ======================================================================


class TestEnumeratorBoundary:
    def test_session_at_exact_ttl_boundary_is_expired_inclusive(
        self, reports_repo: SessionReportsRepository
    ) -> None:
        """A row with created_at exactly == (now - ttl) IS expired.
        Strict inequality would let rows linger one tick past their
        advertised TTL."""
        boundary_ts = NOW - timedelta(days=30)
        reports_repo.upsert(
            _make_report(session_id="sess-on-boundary", created_at=boundary_ts)
        )
        result = enumerate_expired_analyses(
            reports_repo,
            analysis_ttl_days=30,
            now=NOW,
        )
        assert len(result) == 1
        assert result[0].session_id == "sess-on-boundary"

    def test_session_one_second_younger_than_boundary_is_not_expired(
        self, reports_repo: SessionReportsRepository
    ) -> None:
        boundary_ts = NOW - timedelta(days=30) + timedelta(seconds=1)
        reports_repo.upsert(
            _make_report(session_id="sess-just-young", created_at=boundary_ts)
        )
        result = enumerate_expired_analyses(
            reports_repo,
            analysis_ttl_days=30,
            now=NOW,
        )
        assert result == []


class TestEnumeratorStableOrder:
    def test_enumerator_returns_sessions_sorted_by_session_id_ascending(
        self, reports_repo: SessionReportsRepository
    ) -> None:
        old_ts = NOW - timedelta(days=400)
        for sid in ("sess-z", "sess-a", "sess-m"):
            reports_repo.upsert(_make_report(session_id=sid, created_at=old_ts))

        result = enumerate_expired_analyses(
            reports_repo,
            analysis_ttl_days=30,
            now=NOW,
        )
        assert [r.session_id for r in result] == ["sess-a", "sess-m", "sess-z"]


# ======================================================================
# Happy path
# ======================================================================


class TestHappyPath:
    def test_purger_reaps_session_reports_and_behavior_flags(
        self,
        reports_repo: SessionReportsRepository,
        flags_repo: BehaviorFlagsRepository,
        purger: AnalysisResultsPurger,
    ) -> None:
        """B-H2: given K expired sessions, purge removes K session_reports
        rows + all matching behavior_flags rows."""
        old_ts = NOW - timedelta(days=400)
        sessions = ("sess-1", "sess-2", "sess-3")
        for sid in sessions:
            reports_repo.upsert(_make_report(session_id=sid, created_at=old_ts))
            for i in range(2):
                flags_repo.insert(
                    _make_flag(
                        session_id=sid,
                        flag_index=i,
                        created_at=old_ts,
                    )
                )

        expired = [
            ExpiredAnalysis(session_id=sid, report_created_at=old_ts)
            for sid in sessions
        ]
        result = purger.purge(expired)

        assert set(result.purged_session_ids) == set(sessions)
        assert result.had_failures is False
        for sid in sessions:
            assert reports_repo.get_for_session(sid) is None
            assert flags_repo.get_session_flags(sid) == []

    def test_purger_respects_ttl_does_not_touch_fresh_sessions(
        self,
        reports_repo: SessionReportsRepository,
        flags_repo: BehaviorFlagsRepository,
        purger: AnalysisResultsPurger,
    ) -> None:
        """A fresh session (not in `expired` input) must remain untouched
        even if it shares a project_id with reaped sessions."""
        old_ts = NOW - timedelta(days=400)
        fresh_ts = NOW - timedelta(days=1)

        reports_repo.upsert(
            _make_report(session_id="sess-old", created_at=old_ts)
        )
        flags_repo.insert(
            _make_flag(session_id="sess-old", created_at=old_ts)
        )
        reports_repo.upsert(
            _make_report(session_id="sess-fresh", created_at=fresh_ts)
        )
        flags_repo.insert(
            _make_flag(session_id="sess-fresh", created_at=fresh_ts)
        )

        # Purger only reaps what enumerator passes — the contract is the
        # input list, NOT a global scan.
        result = purger.purge([
            ExpiredAnalysis(
                session_id="sess-old",
                report_created_at=old_ts,
            )
        ])

        assert "sess-old" in result.purged_session_ids
        assert reports_repo.get_for_session("sess-old") is None
        # Fresh row untouched.
        assert reports_repo.get_for_session("sess-fresh") is not None
        assert len(flags_repo.get_session_flags("sess-fresh")) == 1
