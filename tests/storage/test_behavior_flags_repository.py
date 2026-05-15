"""Death + happy-path tests for BehaviorFlagsRepository (GUR-100 task-2)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

import pytest

from secondsight.analysis.schemas import BehaviorFlag, BehaviorFlagType
from secondsight.storage.behavior_flags_repository import (
    BehaviorFlagsRepository,
)
from secondsight.storage.db_engine import DBEngine


def _now() -> datetime:
    return datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _flag(
    *,
    id: str = "flag-1",
    flag_type: BehaviorFlagType = BehaviorFlagType.UNNECESSARY_READ,
    confidence: Literal["high", "medium", "low"] = "high",
    project_id: str = "proj-1",
    session_id: str = "sess-1",
    event_ids: list[str] | None = None,
) -> BehaviorFlag:
    return BehaviorFlag(
        id=id,
        project_id=project_id,
        session_id=session_id,
        segment_index=1,
        flag_type=flag_type,
        event_ids=event_ids if event_ids is not None else ["e1", "e2"],
        intent_summary="fix bug",
        reason="extraneous read",
        confidence=confidence,
        created_at=_now(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[BehaviorFlagsRepository]:
    eng = DBEngine(tmp_path / "intel.db")
    r = BehaviorFlagsRepository(eng)
    r.create_schema()
    try:
        yield r
    finally:
        eng.dispose()


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_2_1_insert_rejects_model_construct_bypass_invalid_flag_type(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """DT-2.1 — model_construct() bypasses Pydantic; repo defensive
        guard MUST reject invalid enum at insert and persist no row.
        """
        # model_construct bypasses validation entirely.
        bypassed = BehaviorFlag.model_construct(
            id="bypass-1",
            project_id="proj-1",
            session_id="sess-1",
            segment_index=1,
            flag_type="bogus_type",  # not a BehaviorFlagType
            event_ids=["e1"],
            intent_summary="x",
            reason="y",
            confidence="high",
            created_at=_now(),
        )

        with pytest.raises(ValueError) as exc:
            repo.insert(bypassed)
        assert "flag_type" in str(exc.value).lower()

        # Verify zero rows landed.
        rows = repo.get_session_flags("sess-1")
        assert rows == [], (
            "behavior_flags accepted a row with invalid flag_type — "
            "repository defensive guard missing"
        )

    def test_dt_2_2_insert_idempotent_on_id_first_wins(self, repo: BehaviorFlagsRepository) -> None:
        """DT-2.2 — Two insert()s with same id, different flag_type:
        only FIRST persists. ON CONFLICT DO NOTHING contract.
        """
        f1 = _flag(id="flag-x", flag_type=BehaviorFlagType.UNNECESSARY_READ)
        f2 = _flag(id="flag-x", flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION)

        repo.insert(f1)
        repo.insert(f2)

        rows = repo.get_session_flags("sess-1")
        assert len(rows) == 1
        assert rows[0].flag_type is BehaviorFlagType.UNNECESSARY_READ, (
            "ON CONFLICT DO NOTHING failed — second insert overwrote first"
        )

    def test_dt_2_3_insert_then_fresh_repo_round_trip(self, tmp_path: Path) -> None:
        """DT-2.3 — INSERT must commit; a fresh repository instance
        against the same engine must see the row. Detects the silent-
        failure where the INSERT was buffered but never durable.
        """
        eng = DBEngine(tmp_path / "intel.db")
        try:
            r1 = BehaviorFlagsRepository(eng)
            r1.create_schema()
            r1.insert(_flag())

            r2 = BehaviorFlagsRepository(eng)
            rows = r2.get_session_flags("sess-1")
            assert len(rows) == 1
            assert rows[0].id == "flag-1"
        finally:
            eng.dispose()

    def test_dt_2_4_insert_rejects_model_construct_bypass_invalid_confidence(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """Bypass via model_construct on confidence — defensive guard rejects."""
        bypassed = BehaviorFlag.model_construct(
            id="bp-2",
            project_id="proj-1",
            session_id="sess-1",
            segment_index=1,
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["e1"],
            intent_summary="x",
            reason="y",
            confidence="kinda",  # invalid
            created_at=_now(),
        )
        with pytest.raises(ValueError) as exc:
            repo.insert(bypassed)
        assert "confidence" in str(exc.value).lower()


# =====================================================================
# HAPPY PATHS
# =====================================================================


class TestHappyPaths:
    def test_round_trip_preserves_event_ids_and_all_fields(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """Round-trip preserves every field except `created_at` tzinfo.

        SQLite's DateTime column drops tzinfo on read-back (a known
        SQLAlchemy + SQLite limitation; the existing events_repository
        has the same behavior — see tests/storage/test_events_repository.py).
        Whole-model equality includes the wall-clock comparison only;
        tz round-trip is asserted separately.
        """
        original = _flag(event_ids=["e1", "e2", "e3"])
        repo.insert(original)
        rows = repo.get_session_flags("sess-1")
        assert len(rows) == 1
        got = rows[0]

        # Every non-datetime field must round-trip exactly.
        for field in (
            "id",
            "project_id",
            "session_id",
            "segment_index",
            "flag_type",
            "event_ids",
            "intent_summary",
            "reason",
            "confidence",
        ):
            assert getattr(got, field) == getattr(original, field), (
                f"{field} mutated during round-trip"
            )

        # JSON-encoded event_ids preserved as ordered list.
        assert got.event_ids == ["e1", "e2", "e3"]

        # created_at: wall-clock matches; tzinfo dropped (SQLite limitation).
        assert got.created_at.replace(tzinfo=None) == original.created_at.replace(tzinfo=None)

    def test_insert_many_returns_count_and_count_by_type_aggregates(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        flags = [
            _flag(id=f"f-{i}", flag_type=BehaviorFlagType.UNNECESSARY_READ) for i in range(30)
        ] + [
            _flag(
                id=f"g-{i}",
                flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
            )
            for i in range(20)
        ]
        n = repo.insert_many(flags)
        assert n == 50

        counts = repo.count_by_type("proj-1")
        assert counts[BehaviorFlagType.UNNECESSARY_READ] == 30
        assert counts[BehaviorFlagType.REDUNDANT_EXPLORATION] == 20

    def test_get_project_flags_by_type_filters(self, repo: BehaviorFlagsRepository) -> None:
        repo.insert(_flag(id="a", flag_type=BehaviorFlagType.WRONG_TOOL_CHOICE))
        repo.insert(_flag(id="b", flag_type=BehaviorFlagType.UNNECESSARY_READ))
        only_wrong = repo.get_project_flags_by_type("proj-1", BehaviorFlagType.WRONG_TOOL_CHOICE)
        assert len(only_wrong) == 1
        assert only_wrong[0].id == "a"

    def test_create_schema_idempotent(self, tmp_path: Path) -> None:
        eng = DBEngine(tmp_path / "intel.db")
        try:
            r = BehaviorFlagsRepository(eng)
            r.create_schema()
            r.create_schema()  # second call must not raise
        finally:
            eng.dispose()

    def test_insert_many_empty_returns_zero_and_no_rows(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        n = repo.insert_many([])
        assert n == 0
        assert repo.get_session_flags("sess-1") == []
