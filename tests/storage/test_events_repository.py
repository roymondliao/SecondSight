"""Tests for EventsRepository (P1-3). Death tests first."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from secondsight.event import EventType
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from tests.conftest import make_event


@pytest.fixture
def repo(tmp_path: Path):
    eng = DBEngine(tmp_path / "intel.db")
    r = EventsRepository(eng)
    r.create_schema()
    yield r
    eng.dispose()


# ---------------------------------------------------------------------------
# Death tests
# ---------------------------------------------------------------------------


def test_death_idempotent_insert_does_not_overwrite(repo: EventsRepository) -> None:
    """Same `id` twice must be a no-op. Original `data` must survive."""
    e1 = make_event(event_id="evt-1", data={"original": True})
    e2 = make_event(event_id="evt-1", data={"original": False})
    repo.insert(e1)
    repo.insert(e2)
    rows = repo.get_session_events("sess-001")
    assert len(rows) == 1
    assert rows[0].data == {"original": True}


def test_death_unique_session_seq_violation_raises(repo: EventsRepository) -> None:
    """Two different `id`s but same (session_id, sequence_number) MUST raise.
    This is the analysis-correctness boundary; silently dropping is a bug.
    """
    e1 = make_event(event_id="evt-1", session_id="sess-A", sequence_number=5)
    e2 = make_event(event_id="evt-2", session_id="sess-A", sequence_number=5)
    repo.insert(e1)
    with pytest.raises(IntegrityError):
        repo.insert(e2)


def test_death_unicode_and_nested_data_roundtrip(repo: EventsRepository) -> None:
    """JSON data column must round-trip unicode + nested structures losslessly."""
    payload = {
        "unicode": "中文 🚀 ​",
        "nested": {"a": [1, 2, {"b": None, "c": True}]},
        "empty_list": [],
        "empty_dict": {},
    }
    repo.insert(make_event(event_id="u-1", data=payload))
    rows = repo.get_session_events("sess-001")
    assert rows[0].data == payload


def test_death_get_max_segment_index_distinguishes_zero_from_none(
    repo: EventsRepository,
) -> None:
    """Empty session → None. Session with segment_index=0 → 0.
    Confusing them shifts every subsequent index by 1.
    """
    assert repo.get_max_segment_index("never-seen") is None
    repo.insert(make_event(event_id="zs-1", session_id="zero-seg", segment_index=0))
    assert repo.get_max_segment_index("zero-seg") == 0


def test_death_concurrent_unique_violation_one_winner(tmp_path: Path) -> None:
    """Two threads racing on the same (session_id, sequence_number): exactly
    one wins, the loser sees IntegrityError — never silent.
    """
    eng = DBEngine(tmp_path / "intel.db")
    repo = EventsRepository(eng)
    repo.create_schema()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(event_id: str) -> None:
        e = make_event(event_id=event_id, session_id="race", sequence_number=42)
        barrier.wait()
        try:
            repo.insert(e)
            with lock:
                outcomes.append(f"win:{event_id}")
        except IntegrityError:
            with lock:
                outcomes.append(f"lose:{event_id}")

    t1 = threading.Thread(target=attempt, args=("a",))
    t2 = threading.Thread(target=attempt, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    wins = [o for o in outcomes if o.startswith("win:")]
    loses = [o for o in outcomes if o.startswith("lose:")]
    assert len(wins) == 1
    assert len(loses) == 1
    eng.dispose()


def test_death_create_schema_idempotent_with_extra_column(
    tmp_path: Path,
) -> None:
    """create_schema() must be safe against an already-extended table.
    Phase 2 may ALTER TABLE add new columns; replaying create_schema
    must not drop them.
    """
    eng = DBEngine(tmp_path / "intel.db")
    repo = EventsRepository(eng)
    repo.create_schema()

    with eng.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE events ADD COLUMN future_col TEXT")

    # Must not raise; must not drop future_col
    repo.create_schema()

    with eng.engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info(events)").fetchall()
    col_names = {c[1] for c in cols}
    assert "future_col" in col_names
    eng.dispose()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_roundtrip_every_event_type(repo: EventsRepository) -> None:
    for i, et in enumerate(EventType):
        repo.insert(
            make_event(
                event_id=f"e-{i}",
                sequence_number=i,
                event_type=et,
                data={"v": et.value},
            )
        )
    rows = repo.get_session_events("sess-001")
    assert len(rows) == len(list(EventType))
    types = [r.event_type for r in rows]
    assert types == sorted(types, key=lambda et: list(EventType).index(et))


def test_indexes_created(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db")
    repo = EventsRepository(eng)
    repo.create_schema()
    with eng.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_events_session_seq" in names
    assert "idx_events_segment" in names
    assert "idx_events_type" in names
    assert "idx_events_sub_agent" in names
    eng.dispose()


def test_get_session_events_ordered_by_sequence(repo: EventsRepository) -> None:
    for seq in [3, 1, 7, 2]:
        repo.insert(make_event(event_id=f"o-{seq}", sequence_number=seq))
    rows = repo.get_session_events("sess-001")
    assert [r.sequence_number for r in rows] == [1, 2, 3, 7]


def test_get_segment_events(repo: EventsRepository) -> None:
    for i in range(6):
        repo.insert(
            make_event(
                event_id=f"s-{i}",
                sequence_number=i,
                segment_index=i % 2,
            )
        )
    seg_0 = repo.get_segment_events("sess-001", 0)
    seg_1 = repo.get_segment_events("sess-001", 1)
    assert len(seg_0) == 3
    assert len(seg_1) == 3


def test_insert_many_returns_count(repo: EventsRepository) -> None:
    events = [make_event(event_id=f"m-{i}", sequence_number=i) for i in range(50)]
    n = repo.insert_many(events)
    assert n == 50


def test_exists(repo: EventsRepository) -> None:
    repo.insert(make_event(event_id="x-1"))
    assert repo.exists("x-1")
    assert not repo.exists("nope")


def test_subagent_fields_roundtrip(repo: EventsRepository) -> None:
    repo.insert(
        make_event(
            event_id="sa-1",
            sequence_number=0,
            sub_agent_id="sa_001",
            depth=2,
        )
    )
    repo.insert(
        make_event(
            event_id="sa-main",
            sequence_number=1,
            sub_agent_id=None,
            depth=0,
        )
    )
    rows = repo.get_session_events("sess-001")
    sa_row = [r for r in rows if r.id == "sa-1"][0]
    main_row = [r for r in rows if r.id == "sa-main"][0]
    assert sa_row.sub_agent_id == "sa_001"
    assert sa_row.depth == 2
    assert main_row.sub_agent_id is None
    assert main_row.depth == 0
