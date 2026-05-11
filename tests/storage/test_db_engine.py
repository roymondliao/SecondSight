"""Tests for DBEngine (P1-2). Death tests first."""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

import pytest
import sqlalchemy as sa

from secondsight.storage.db_engine import (
    DBEngine,
    StoragePragmaMismatchError,
    StorageSettings,
)


# ---------------------------------------------------------------------------
# Death tests
# ---------------------------------------------------------------------------


def test_death_wal_mode_silently_rejected_raises(tmp_path: Path) -> None:
    """If the connection's WAL request silently downgrades (e.g., the DB
    lives on a filesystem that doesn't support shared memory), construction
    MUST raise. Simulate by injecting a broken listener that applies DELETE.
    """

    class BrokenDBEngine(DBEngine):
        @staticmethod
        def _make_connect_listener(settings):
            def listener(dbapi_connection, _record) -> None:
                cur = dbapi_connection.cursor()
                try:
                    # Wrong on purpose — silent downgrade scenario
                    cur.execute("PRAGMA journal_mode=DELETE;")
                    cur.execute("PRAGMA busy_timeout=5000;")
                    cur.execute("PRAGMA synchronous=NORMAL;")
                    cur.execute("PRAGMA wal_autocheckpoint=1000;")
                    cur.execute(f"PRAGMA cache_size=-{settings.cache_size_mb * 1000};")
                finally:
                    cur.close()

            return listener

    with pytest.raises(StoragePragmaMismatchError):
        BrokenDBEngine(tmp_path / "intel.db")


def test_death_settings_is_frozen(tmp_path: Path) -> None:
    s = StorageSettings(cache_size_mb=64)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.cache_size_mb = 128  # type: ignore[misc]


def test_death_settings_validates_cache_size_positive() -> None:
    with pytest.raises(ValueError):
        StorageSettings(cache_size_mb=0)
    with pytest.raises(ValueError):
        StorageSettings(cache_size_mb=-5)


def test_death_engine_after_dispose_raises(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db")
    eng.dispose()
    with pytest.raises(RuntimeError):
        _ = eng.engine


def test_death_verify_pragmas_raises_on_mismatch(tmp_path: Path) -> None:
    """`verify_pragmas` MUST raise — not silently return a dict the caller
    might forget to check. Construct a DBEngine without registering the
    listener, so connections lack PRAGMAs.
    """
    # Bypass __init__'s listener registration to construct an engine
    # with no PRAGMA listener attached.
    eng = DBEngine.__new__(DBEngine)
    eng._settings = StorageSettings()
    eng._db_path = tmp_path / "intel.db"
    eng._db_path.parent.mkdir(parents=True, exist_ok=True)
    eng._engine = sa.create_engine(f"sqlite:///{eng._db_path}", future=True)
    eng._connect_listener = lambda *a, **k: None

    try:
        with pytest.raises(StoragePragmaMismatchError):
            eng.verify_pragmas()
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_pragmas_applied_on_construction(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db")
    pragmas = eng.verify_pragmas()
    assert pragmas["journal_mode"].lower() == "wal"
    assert int(pragmas["busy_timeout"]) == 5000
    # synchronous: NORMAL == 1
    assert int(pragmas["synchronous"]) == 1
    assert int(pragmas["wal_autocheckpoint"]) == 1000
    eng.dispose()


def test_cache_size_pragma_uses_settings(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db", StorageSettings(cache_size_mb=128))
    pragmas = eng.verify_pragmas()
    # SQLite returns negative kibibytes when cache_size was set with -N
    assert int(pragmas["cache_size"]) == -128_000
    eng.dispose()


def test_db_file_and_parent_created(tmp_path: Path) -> None:
    db_path = tmp_path / "deeply" / "nested" / "intel.db"
    eng = DBEngine(db_path)
    assert db_path.exists()
    eng.dispose()


def test_dispose_is_idempotent(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db")
    eng.dispose()
    eng.dispose()  # must not raise


def test_concurrent_threads_each_get_pragmas(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db")
    errors: list[str] = []

    def worker() -> None:
        try:
            with eng.engine.connect() as conn:
                row = conn.exec_driver_sql("PRAGMA journal_mode;").fetchone()
                if not row or row[0].lower() != "wal":
                    errors.append(f"thread saw {row}")
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    eng.dispose()


def test_engine_sqlalchemy_url_format(tmp_path: Path) -> None:
    eng = DBEngine(tmp_path / "intel.db")
    assert str(eng.engine.url).startswith("sqlite:///")
    eng.dispose()
