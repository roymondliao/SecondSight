"""Death + happy-path tests for
BehaviorFlagsRepository.count_per_session_for_project (GUR-104 task-1).

DC-7 (LIMIT scope): the SQL must apply LIMIT to the session set first,
then JOIN behavior_flags. A naive `JOIN ... LIMIT N` returns N flag rows,
not N sessions, which would silently corrupt the trends endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest

from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    SessionReport,
)
from secondsight.storage.behavior_flags_repository import (
    BehaviorFlagsRepository,
)
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.session_reports_repository import (
    SessionReportsRepository,
)


_BASE = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def _flag(
    *,
    project_id: str,
    session_id: str,
    flag_type: BehaviorFlagType,
    minute: int,
    seq: int = 0,
) -> BehaviorFlag:
    return BehaviorFlag(
        id=f"flag-{session_id}-{flag_type.value}-{seq}",
        project_id=project_id,
        session_id=session_id,
        segment_index=0,
        flag_type=flag_type,
        event_ids=["e1"],
        intent_summary="x",
        reason="y",
        confidence="high",
        created_at=_BASE + timedelta(minutes=minute),
    )


def _report(
    *,
    project_id: str,
    session_id: str,
    minute: int,
) -> SessionReport:
    return SessionReport(
        id=f"rep-{session_id}",
        project_id=project_id,
        session_id=session_id,
        analysis_run_id=f"run-{session_id}",
        headline=f"Session {session_id}",
        key_findings=["one"],
        body="body",
        created_at=_BASE + timedelta(minutes=minute),
        updated_at=_BASE + timedelta(minutes=minute),
    )


@pytest.fixture
def repos(
    tmp_path: Path,
) -> Iterator[tuple[BehaviorFlagsRepository, SessionReportsRepository]]:
    eng = DBEngine(tmp_path / "intel.db")
    flags = BehaviorFlagsRepository(eng)
    reports = SessionReportsRepository(eng)
    flags.create_schema()
    reports.create_schema()
    try:
        yield flags, reports
    finally:
        eng.dispose()


# =====================================================================
# DEATH TESTS — silent failure paths first
# =====================================================================


class TestDeathPaths:
    def test_dt_1_1_limit_applies_to_session_set_not_joined_flags(
        self,
        repos: tuple[BehaviorFlagsRepository, SessionReportsRepository],
    ) -> None:
        """DT-1.1 (DC-7) — 50 sessions x 5 flags. A naive JOIN+LIMIT 10
        would return 10 flag rows (which roll up to ≤ 10 sessions but
        miss flag-type breakdown).  The CTE/SUBQUERY shape must
        return exactly 10 sessions, each with their full 5 flags.
        """
        flags_repo, reports_repo = repos
        project_id = "proj-A"

        # Insert 50 sessions, each with 5 flags of distinct flag types.
        for i in range(50):
            session_id = f"sess-{i:02d}"
            reports_repo.upsert(
                _report(
                    project_id=project_id,
                    session_id=session_id,
                    minute=i,  # session i is more recent than i-1
                )
            )
            for j, ft in enumerate(list(BehaviorFlagType)[:5]):
                flags_repo.insert(
                    _flag(
                        project_id=project_id,
                        session_id=session_id,
                        flag_type=ft,
                        minute=i,
                        seq=j,
                    )
                )

        result = flags_repo.count_per_session_for_project(
            project_id, limit=10
        )

        # DC-7 assertion: exactly 10 SESSIONS, not 10 flags.
        assert len(result) == 10, (
            f"DC-7 silent failure: expected 10 sessions but got {len(result)}. "
            "LIMIT was applied to the joined flags table, not the session set."
        )

        # Each session's bucket carries all 5 flags it owns.
        total_flag_rows = sum(
            sum(b.counts_by_type.values()) for b in result
        )
        assert total_flag_rows == 50, (
            f"Expected 50 total flag counts (10 sessions x 5 each); "
            f"got {total_flag_rows}. Suggests row-level LIMIT bug."
        )

    def test_dt_1_2_sessions_ordered_by_analyzed_at_desc(
        self,
        repos: tuple[BehaviorFlagsRepository, SessionReportsRepository],
    ) -> None:
        """DT-1.2 — most-recently-analyzed session must be first."""
        flags_repo, reports_repo = repos
        project_id = "proj-A"

        # Insert 5 sessions with strictly increasing created_at.
        for i in range(5):
            sid = f"sess-{i}"
            reports_repo.upsert(
                _report(project_id=project_id, session_id=sid, minute=i)
            )
            flags_repo.insert(
                _flag(
                    project_id=project_id,
                    session_id=sid,
                    flag_type=BehaviorFlagType.UNNECESSARY_READ,
                    minute=i,
                )
            )

        result = flags_repo.count_per_session_for_project(
            project_id, limit=5
        )

        analyzed_ats = [b.analyzed_at for b in result]
        assert analyzed_ats == sorted(analyzed_ats, reverse=True), (
            f"Sessions not ordered by analyzed_at DESC: {analyzed_ats}"
        )
        assert result[0].session_id == "sess-4"
        assert result[-1].session_id == "sess-0"

    def test_dt_1_3_zero_flag_session_appears_with_empty_counts(
        self,
        repos: tuple[BehaviorFlagsRepository, SessionReportsRepository],
    ) -> None:
        """DT-1.3 — a session with a session_reports row but ZERO
        behavior_flags must still appear in the result; the
        counts_by_type dict for that session is empty.

        If LEFT JOIN was downgraded to INNER JOIN, the session is
        silently dropped from trends — the dashboard renders a
        chart that pretends the session never existed.
        """
        flags_repo, reports_repo = repos
        project_id = "proj-A"

        # Two sessions: one with a flag, one without.
        reports_repo.upsert(
            _report(project_id=project_id, session_id="sess-with", minute=1)
        )
        flags_repo.insert(
            _flag(
                project_id=project_id,
                session_id="sess-with",
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                minute=1,
            )
        )
        reports_repo.upsert(
            _report(
                project_id=project_id, session_id="sess-empty", minute=2
            )
        )

        result = flags_repo.count_per_session_for_project(
            project_id, limit=10
        )

        ids = {b.session_id for b in result}
        assert ids == {"sess-with", "sess-empty"}

        empty = next(b for b in result if b.session_id == "sess-empty")
        assert empty.counts_by_type == {}, (
            f"Zero-flag session must have empty counts_by_type; "
            f"got {empty.counts_by_type}. INNER JOIN bug suspected."
        )

    def test_dt_1_4_cross_project_isolation(
        self,
        repos: tuple[BehaviorFlagsRepository, SessionReportsRepository],
    ) -> None:
        """DT-1.4 — querying project A must NOT return any project B row.
        DC-1 family: cross-project leak via missing WHERE filter.
        """
        flags_repo, reports_repo = repos

        # Project A: 1 session, 1 flag.
        reports_repo.upsert(
            _report(project_id="proj-A", session_id="A-1", minute=1)
        )
        flags_repo.insert(
            _flag(
                project_id="proj-A",
                session_id="A-1",
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                minute=1,
            )
        )

        # Project B: 3 sessions, 9 flags.
        for i in range(3):
            sid = f"B-{i}"
            reports_repo.upsert(
                _report(project_id="proj-B", session_id=sid, minute=10 + i)
            )
            for j in range(3):
                flags_repo.insert(
                    _flag(
                        project_id="proj-B",
                        session_id=sid,
                        flag_type=BehaviorFlagType.UNNECESSARY_READ,
                        minute=10 + i,
                        seq=j,
                    )
                )

        result_a = flags_repo.count_per_session_for_project(
            "proj-A", limit=50
        )
        result_b = flags_repo.count_per_session_for_project(
            "proj-B", limit=50
        )

        assert {b.session_id for b in result_a} == {"A-1"}
        assert {b.session_id for b in result_b} == {"B-0", "B-1", "B-2"}

        # And: no project-A session leaks into B's per-session flag join.
        for b in result_b:
            assert not b.session_id.startswith("A-")


# =====================================================================
# HAPPY PATH
# =====================================================================


class TestHappyPath:
    def test_hp_1_1_known_distribution(
        self,
        repos: tuple[BehaviorFlagsRepository, SessionReportsRepository],
    ) -> None:
        """HP-1.1 — 5 sessions with distinct flag-type distributions;
        assert exact counts per session per type.
        """
        flags_repo, reports_repo = repos
        project_id = "proj-A"

        # Distribution per session_id → list of flag types
        distribution = {
            "S1": [
                BehaviorFlagType.UNNECESSARY_READ,
                BehaviorFlagType.UNNECESSARY_READ,
            ],
            "S2": [BehaviorFlagType.REDUNDANT_EXPLORATION],
            "S3": [
                BehaviorFlagType.UNNECESSARY_READ,
                BehaviorFlagType.REDUNDANT_EXPLORATION,
                BehaviorFlagType.REDUNDANT_EXPLORATION,
            ],
            "S4": [],  # zero-flag session
            "S5": [BehaviorFlagType.UNNECESSARY_READ],
        }

        for i, (sid, flag_list) in enumerate(distribution.items()):
            reports_repo.upsert(
                _report(project_id=project_id, session_id=sid, minute=i)
            )
            for j, ft in enumerate(flag_list):
                flags_repo.insert(
                    _flag(
                        project_id=project_id,
                        session_id=sid,
                        flag_type=ft,
                        minute=i,
                        seq=j,
                    )
                )

        result = flags_repo.count_per_session_for_project(
            project_id, limit=10
        )
        by_id = {b.session_id: b.counts_by_type for b in result}

        assert by_id["S1"] == {BehaviorFlagType.UNNECESSARY_READ: 2}
        assert by_id["S2"] == {BehaviorFlagType.REDUNDANT_EXPLORATION: 1}
        assert by_id["S3"] == {
            BehaviorFlagType.UNNECESSARY_READ: 1,
            BehaviorFlagType.REDUNDANT_EXPLORATION: 2,
        }
        assert by_id["S4"] == {}
        assert by_id["S5"] == {BehaviorFlagType.UNNECESSARY_READ: 1}
