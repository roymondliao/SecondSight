"""Tests for ObservationPipeline (P1-4) — the durability contract.

Death tests come first. The most important one is #1: DB failure must
land in the sync log, not be silently swallowed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from secondsight.event import Event
from secondsight.observation.pipeline import ObservationPipeline
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.sync_log import SyncLog
from tests.conftest import make_event

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pipeline(tmp_path: Path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    rts = RawTraceStore(project_root)
    eng = DBEngine(tmp_path / "intel.db")
    repo = EventsRepository(eng)
    repo.create_schema()
    sl = SyncLog(project_root / "sync_failures.jsonl")
    p = ObservationPipeline(rts, repo, sl)
    yield p, rts, repo, sl, project_root
    eng.dispose()


# ---------------------------------------------------------------------------
# Death tests — the durability contract under failure
# ---------------------------------------------------------------------------


async def test_death_db_failure_lands_in_sync_log(pipeline) -> None:
    """The most important test in Phase 1. DB failure → sync log entry.
    Silently dropping is unacceptable."""
    p, rts, repo, sl, _ = pipeline
    event = make_event(event_id="db-fail-1")

    def boom(e: Event) -> None:
        raise OperationalError("db", {}, Exception("locked"))

    with patch.object(repo, "insert", side_effect=boom):
        await p.ingest(event)

    # Raw trace landed
    assert rts.event_path(event).exists()
    # Sync log has one entry
    entries = list(sl.iter_pending())
    assert len(entries) == 1
    assert entries[0].event_id == "db-fail-1"
    assert "OperationalError" in entries[0].error_class
    # DB has no row
    assert not repo.exists("db-fail-1")


async def test_death_fs_failure_does_not_call_db(pipeline) -> None:
    """FS comes first. If FS raises, DB INSERT must NOT have been called."""
    p, rts, repo, sl, _ = pipeline
    event = make_event(event_id="fs-fail-1")

    db_calls: list[Event] = []
    real_insert = repo.insert
    repo.insert = lambda e: db_calls.append(e) or real_insert(e)

    async def fs_boom(_e: Event) -> Path:
        raise OSError(28, "No space left on device")

    with patch.object(rts, "write", side_effect=fs_boom):
        with pytest.raises(OSError):
            await p.ingest(event)

    assert db_calls == []
    assert not repo.exists("fs-fail-1")


async def test_death_sync_log_failure_propagates_loudly(pipeline) -> None:
    """If both DB AND sync log fail, we have lost the recovery path.
    Pipeline must surface this as CRITICAL and re-raise.
    """
    p, rts, repo, sl, _ = pipeline
    event = make_event(event_id="catastrophe-1")

    def boom_db(_e: Event) -> None:
        raise OperationalError("db", {}, Exception("locked"))

    def boom_log(*a: Any, **k: Any) -> None:
        raise OSError("sync log filesystem also dead")

    with (
        patch.object(repo, "insert", side_effect=boom_db),
        patch.object(sl, "record_failure", side_effect=boom_log),
    ):
        with pytest.raises(OSError, match="sync log"):
            await p.ingest(event)

    # Raw trace still landed (FS came first and succeeded)
    assert rts.event_path(event).exists()


async def test_death_concurrent_ingest_no_data_loss(pipeline) -> None:
    """asyncio.gather of 50 distinct events must produce all 50 traces
    + 50 DB rows.
    """
    p, rts, repo, sl, _ = pipeline
    events = [make_event(event_id=f"c-{i}", sequence_number=i) for i in range(50)]
    await asyncio.gather(*(p.ingest(e) for e in events))

    rows = repo.get_session_events("sess-001")
    assert len(rows) == 50
    assert {r.id for r in rows} == {f"c-{i}" for i in range(50)}
    # Sync log empty on the happy path
    assert list(sl.iter_pending()) == []


async def test_death_keyboard_interrupt_during_db_insert_keeps_raw_trace(
    pipeline,
) -> None:
    """KeyboardInterrupt during DB INSERT — raw trace must remain on disk.
    Pipeline must NOT attempt to roll back the FS write."""
    p, rts, repo, sl, _ = pipeline
    event = make_event(event_id="ki-1")

    def boom(_e: Event) -> None:
        raise KeyboardInterrupt

    with patch.object(repo, "insert", side_effect=boom):
        with pytest.raises(KeyboardInterrupt):
            await p.ingest(event)

    assert rts.event_path(event).exists()


async def test_death_sync_log_entry_is_atomic_per_line(tmp_path: Path) -> None:
    """A truncated sync log file (process killed mid-write) must not
    poison `iter_pending`. Either the line is fully present or it's gone.
    """
    sl = SyncLog(tmp_path / "sync.jsonl")
    sl.record_failure("e-1", tmp_path / "trace1.json", RuntimeError("x"))
    sl.record_failure("e-2", tmp_path / "trace2.json", RuntimeError("y"))

    raw = (tmp_path / "sync.jsonl").read_bytes()
    # Truncate halfway through the second line
    cut = len(raw) - 8
    (tmp_path / "sync.jsonl").write_bytes(raw[:cut])

    entries = list(sl.iter_pending())
    # The first complete line is preserved; the truncated line is dropped
    assert len(entries) == 1
    assert entries[0].event_id == "e-1"


# ---------------------------------------------------------------------------
# Unit tests — happy-path + mixed scenarios
# ---------------------------------------------------------------------------


async def test_happy_path_writes_both_layers(pipeline) -> None:
    p, rts, repo, sl, _ = pipeline
    event = make_event(event_id="ok-1")
    await p.ingest(event)
    assert rts.event_path(event).exists()
    assert repo.exists("ok-1")
    assert list(sl.iter_pending()) == []


async def test_mixed_success_and_db_failures(pipeline) -> None:
    p, rts, repo, sl, _ = pipeline
    events = [make_event(event_id=f"m-{i}", sequence_number=i) for i in range(10)]

    failing_ids = {"m-2", "m-5", "m-7"}
    real_insert = repo.insert

    def maybe_fail(e: Event) -> None:
        if e.id in failing_ids:
            raise OperationalError("db", {}, Exception("nope"))
        real_insert(e)

    with patch.object(repo, "insert", side_effect=maybe_fail):
        for e in events:
            await p.ingest(e)

    # All 10 raw traces on disk
    for e in events:
        assert rts.event_path(e).exists()
    # 7 in DB
    assert len(repo.get_session_events("sess-001")) == 7
    # 3 in sync log
    pending = list(sl.iter_pending())
    assert {p.event_id for p in pending} == failing_ids


async def test_iter_pending_returns_in_append_order(tmp_path: Path) -> None:
    sl = SyncLog(tmp_path / "sl.jsonl")
    for i in range(5):
        sl.record_failure(f"e-{i}", tmp_path / f"t-{i}.json", ValueError(str(i)))
    ids = [p.event_id for p in sl.iter_pending()]
    assert ids == ["e-0", "e-1", "e-2", "e-3", "e-4"]


async def test_sync_log_record_includes_raw_trace_path(tmp_path: Path) -> None:
    sl = SyncLog(tmp_path / "sl.jsonl")
    p = tmp_path / "events" / "abc.json"
    sl.record_failure("e-1", p, RuntimeError("boom"))
    line = (tmp_path / "sl.jsonl").read_text().strip()
    obj = json.loads(line)
    assert obj["event_id"] == "e-1"
    assert obj["raw_trace_path"] == str(p)
    assert obj["error_class"] == "RuntimeError"
    assert "boom" in obj["error_message"]
    assert "timestamp" in obj
