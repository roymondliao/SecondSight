"""Death tests for RawTracesPurger — task-A4 of GUR-147.

Samsara discipline: death tests first.

Death case from
``changes/2026-05-06_gur-107_phase3a-retention-observation-api/2-plan.md`` §5:
    DC-5: ``RawTracesPurger`` partial failure (DB delete throws after
          FS files removed) leaves a structured ERROR log with the
          affected session_id and surfaces a non-zero exit code from
          the CLI (silent FS/DB drift is unacceptable).

The purger itself does not call sys.exit — that's the CLI's job
(task-A6). Here we pin the contract the purger exposes so the CLI
can act on it: ``PurgeResult.had_failures is True`` and a structured
ERROR log line names the session_id and stage.

D3 (FS first, DB second): if FS removal fails, the DB row MUST stay
intact for that session (otherwise the next reap can never re-attempt
the FS removal because the enumeration query has nothing to match).

We also pin the empty-input no-op and the idempotent absent-FS-dir
path: an operator may have already manually cleared a session, and
the purger must treat that as a normal completion (clear the DB row,
no log noise, no failure).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from secondsight.event import EventType
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.events_table import events as events_table
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.retention import (
    ExpiredSession,
    PurgeFailure,
    PurgeResult,
    RawTracesPurger,
)
from tests.conftest import make_event

UTC = timezone.utc
NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# Fixtures: real EventsRepository + RawTraceStore over tmp_path.
# ----------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def repo(project_dir: Path) -> EventsRepository:
    db = DBEngine(db_path=project_dir / "intel.db")
    r = EventsRepository(db_engine=db)
    r.create_schema()
    return r


@pytest.fixture
def store(project_dir: Path) -> RawTraceStore:
    return RawTraceStore(project_root=project_dir)


@pytest.fixture
def purger(repo: EventsRepository, store: RawTraceStore) -> RawTracesPurger:
    return RawTracesPurger(repo=repo, raw_trace_store=store)


def _seed_session(
    repo: EventsRepository,
    store: RawTraceStore,
    *,
    session_id: str,
    last_ts: datetime,
    event_count: int = 1,
) -> ExpiredSession:
    """Seed `event_count` events ending at `last_ts` for a session.

    Writes both DB rows AND filesystem JSON files via the real
    RawTraceStore — purge must clear both.
    """
    import asyncio

    for i in range(event_count):
        ts = last_ts - timedelta(seconds=event_count - 1 - i)
        ev = make_event(
            event_id=f"{session_id}-{i:04d}",
            session_id=session_id,
            sequence_number=i,
            timestamp=ts,
            event_type=EventType.USER_PROMPT,
        )
        repo.insert(ev)
        asyncio.run(store.write(ev))
    return ExpiredSession(session_id=session_id, last_event_at=last_ts)


def _session_dir(store: RawTraceStore, session_id: str) -> Path:
    return store.project_root / "sessions" / session_id


def _db_event_count(repo: EventsRepository, session_id: str) -> int:
    stmt = (
        sa.select(sa.func.count())
        .select_from(events_table)
        .where(events_table.c.session_id == session_id)
    )
    with repo._db.engine.connect() as conn:  # noqa: SLF001
        return int(conn.execute(stmt).scalar() or 0)


# ----------------------------------------------------------------------
# Happy path — both sides removed, no failures.
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_empty_input_is_noop(self, purger: RawTracesPurger) -> None:
        result = purger.purge([])
        assert result == PurgeResult(purged_session_ids=(), failures=())
        assert result.had_failures is False

    def test_two_sessions_both_sides_removed(
        self,
        repo: EventsRepository,
        store: RawTraceStore,
        purger: RawTracesPurger,
    ) -> None:
        old_ts = NOW - timedelta(days=100)
        s1 = _seed_session(repo, store, session_id="s1", last_ts=old_ts, event_count=2)
        s2 = _seed_session(repo, store, session_id="s2", last_ts=old_ts, event_count=3)

        # Sanity: data exists on both sides.
        assert _session_dir(store, "s1").exists()
        assert _session_dir(store, "s2").exists()
        assert _db_event_count(repo, "s1") == 2
        assert _db_event_count(repo, "s2") == 3

        result = purger.purge([s1, s2])

        assert result.had_failures is False
        assert set(result.purged_session_ids) == {"s1", "s2"}
        assert result.failures == ()
        # FS gone.
        assert not _session_dir(store, "s1").exists()
        assert not _session_dir(store, "s2").exists()
        # DB gone.
        assert _db_event_count(repo, "s1") == 0
        assert _db_event_count(repo, "s2") == 0

    def test_absent_fs_dir_is_idempotent_no_failure(
        self,
        repo: EventsRepository,
        store: RawTraceStore,
        purger: RawTracesPurger,
    ) -> None:
        # Seed both sides, then manually nuke the FS dir to simulate an
        # operator who already cleaned it. DB row remains.
        old_ts = NOW - timedelta(days=100)
        s1 = _seed_session(repo, store, session_id="s1", last_ts=old_ts)
        import shutil

        shutil.rmtree(_session_dir(store, "s1"))

        result = purger.purge([s1])

        assert result.had_failures is False
        assert result.purged_session_ids == ("s1",)
        # DB row was still cleared even though FS was already absent.
        assert _db_event_count(repo, "s1") == 0


# ----------------------------------------------------------------------
# DC-5 — DB delete throws AFTER FS files removed.
# ----------------------------------------------------------------------


class TestDC5DbFailureAfterFsRemoval:
    def test_db_failure_logs_session_id_and_marks_failure(
        self,
        repo: EventsRepository,
        store: RawTraceStore,
        purger: RawTracesPurger,
        propagate_loguru_to_caplog: None,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old_ts = NOW - timedelta(days=100)
        s1 = _seed_session(repo, store, session_id="s1", last_ts=old_ts, event_count=2)

        # Force the DB delete to raise.
        from secondsight.storage import retention as ret_mod

        original = ret_mod._delete_db_events_for_session

        def boom(repo, session_id):  # noqa: ARG001
            raise sa.exc.OperationalError("DELETE", {}, Exception("disk I/O error"))

        monkeypatch.setattr(ret_mod, "_delete_db_events_for_session", boom)

        import logging

        with caplog.at_level(logging.ERROR):
            result = purger.purge([s1])

        # FS already gone — that's the explicit acceptance of D3.
        assert not _session_dir(store, "s1").exists()
        # Failure surfaced.
        assert result.had_failures is True
        assert result.purged_session_ids == ()
        assert len(result.failures) == 1
        f = result.failures[0]
        assert isinstance(f, PurgeFailure)
        assert f.session_id == "s1"
        assert f.stage == "database"
        # Structured ERROR log names the session.
        assert any(
            "s1" in record.getMessage() and record.levelname == "ERROR" for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

        # Restore for any later state checks the test runner does.
        monkeypatch.setattr(ret_mod, "_delete_db_events_for_session", original)


# ----------------------------------------------------------------------
# FS failure — DB row MUST stay so next reap re-attempts.
# ----------------------------------------------------------------------


class TestFsFailureLeavesDbRowIntact:
    def test_fs_failure_does_not_delete_db_row(
        self,
        repo: EventsRepository,
        store: RawTraceStore,
        purger: RawTracesPurger,
        propagate_loguru_to_caplog: None,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old_ts = NOW - timedelta(days=100)
        s1 = _seed_session(repo, store, session_id="s1", last_ts=old_ts, event_count=2)

        # Force the FS removal to raise.
        from secondsight.storage import retention as ret_mod

        def boom(store, session_id):  # noqa: ARG001
            raise OSError("permission denied")

        monkeypatch.setattr(ret_mod, "_delete_fs_session", boom)

        import logging

        with caplog.at_level(logging.ERROR):
            result = purger.purge([s1])

        assert result.had_failures is True
        assert result.purged_session_ids == ()
        assert len(result.failures) == 1
        f = result.failures[0]
        assert f.session_id == "s1"
        assert f.stage == "filesystem"

        # DB row remains so the NEXT reap can re-try; FS still there too.
        assert _db_event_count(repo, "s1") == 2
        assert _session_dir(store, "s1").exists()


# ----------------------------------------------------------------------
# Mixed batch — partial success.
# ----------------------------------------------------------------------


class TestMixedBatchPartialSuccess:
    def test_one_session_db_fails_others_succeed(
        self,
        repo: EventsRepository,
        store: RawTraceStore,
        purger: RawTracesPurger,
        propagate_loguru_to_caplog: None,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old_ts = NOW - timedelta(days=100)
        s1 = _seed_session(repo, store, session_id="s1", last_ts=old_ts)
        s2 = _seed_session(repo, store, session_id="s2", last_ts=old_ts)
        s3 = _seed_session(repo, store, session_id="s3", last_ts=old_ts)

        from secondsight.storage import retention as ret_mod

        original = ret_mod._delete_db_events_for_session

        def gated(repo, session_id):
            if session_id == "s2":
                raise sa.exc.OperationalError("DELETE", {}, Exception("synthetic"))
            return original(repo, session_id)

        monkeypatch.setattr(ret_mod, "_delete_db_events_for_session", gated)

        import logging

        with caplog.at_level(logging.ERROR):
            result = purger.purge([s1, s2, s3])

        assert result.had_failures is True
        assert set(result.purged_session_ids) == {"s1", "s3"}
        assert len(result.failures) == 1
        assert result.failures[0].session_id == "s2"
        assert result.failures[0].stage == "database"

        # s1 and s3 fully cleaned.
        assert not _session_dir(store, "s1").exists()
        assert not _session_dir(store, "s3").exists()
        assert _db_event_count(repo, "s1") == 0
        assert _db_event_count(repo, "s3") == 0
        # s2: FS gone (D3 — already removed before DB error), DB intact.
        assert not _session_dir(store, "s2").exists()
        assert _db_event_count(repo, "s2") == 1


# ---------------------------------------------------------------------------
# Hardening (review MEDIUM-2): _delete_fs_session re-validates session_id.
# ---------------------------------------------------------------------------


class TestUnsafeSessionIdRefused:
    """The purger must NEVER rmtree on an unsafe session_id, even if one
    somehow lands in the events table. The write path validates today,
    but this is the destructive primitive — defense in depth at the
    boundary keeps a future writer or DB tampering from triggering
    traversal at shutil.rmtree time.
    """

    def test_unsafe_session_id_surfaces_as_filesystem_failure(
        self,
        repo: EventsRepository,
        store: RawTraceStore,
        purger: RawTracesPurger,
    ) -> None:
        bad = ExpiredSession(session_id="../../escape", last_event_at=NOW)

        escape = store.project_root.parent / "escape"
        assert not escape.exists()

        result = purger.purge([bad])

        assert result.had_failures is True
        assert result.purged_session_ids == ()
        assert len(result.failures) == 1
        assert result.failures[0].stage == "filesystem"
        assert "unsafe session_id" in result.failures[0].error
        assert not escape.exists()
