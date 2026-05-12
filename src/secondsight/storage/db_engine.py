"""DBEngine — SQLAlchemy 2.0 Core engine factory with PRAGMA enforcement (P1-2).

Per SD §3.5, four PRAGMAs are hard-coded best practice and `cache_size`
is configurable. The engine REJECTS connections that fail to apply WAL,
because a silently-downgraded journal_mode invalidates the concurrency
contract that hooks (writer) and analysis (reader) rely on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_HARDCODED_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("busy_timeout", "5000"),
    ("synchronous", "NORMAL"),
    ("wal_autocheckpoint", "1000"),
)


class StoragePragmaMismatchError(RuntimeError):
    """Raised when a PRAGMA's read-back value does not match the
    configured value. This is the signal that the underlying filesystem
    rejected the desired SQLite mode (e.g., NFS rejecting WAL).
    """


@dataclass(frozen=True)
class StorageSettings:
    cache_size_mb: int = 64

    def __post_init__(self) -> None:
        if self.cache_size_mb <= 0:
            raise ValueError(f"cache_size_mb must be > 0, got {self.cache_size_mb}")


class DBEngine:
    """Owns a SQLAlchemy Engine for one per-project intelligence.db.

    Hard-coded PRAGMAs are applied on every new connection via the
    `connect` event listener; `verify_pragmas()` is called in __init__
    to fail fast if the underlying filesystem rejected the configuration.
    """

    def __init__(
        self,
        db_path: Path,
        settings: StorageSettings | None = None,
    ) -> None:
        self._settings = settings or StorageSettings()
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._engine: Engine | None = sa.create_engine(
            f"sqlite:///{self._db_path}",
            future=True,
        )
        self._connect_listener = self._make_connect_listener(self._settings)
        sa.event.listen(self._engine, "connect", self._connect_listener)

        # Fail fast on misconfiguration.
        self.verify_pragmas()

    @staticmethod
    def _make_connect_listener(settings: StorageSettings):
        def _on_connect(dbapi_connection, _connection_record) -> None:
            cur = dbapi_connection.cursor()
            try:
                for pragma, value in _HARDCODED_PRAGMAS:
                    cur.execute(f"PRAGMA {pragma}={value};")
                cur.execute(f"PRAGMA cache_size=-{settings.cache_size_mb * 1000};")
            finally:
                cur.close()

        return _on_connect

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("DBEngine has been disposed; cannot reuse.")
        return self._engine

    def dispose(self) -> None:
        """Close the underlying engine. Idempotent."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def verify_pragmas(self) -> dict[str, str]:
        """Open a fresh connection, read every PRAGMA, validate, return.

        Raises StoragePragmaMismatchError on any mismatch.
        """
        if self._engine is None:
            raise RuntimeError("DBEngine has been disposed; cannot verify.")

        actual: dict[str, str] = {}
        with self._engine.connect() as conn:
            for pragma, _ in _HARDCODED_PRAGMAS:
                row = conn.exec_driver_sql(f"PRAGMA {pragma};").fetchone()
                actual[pragma] = "" if row is None else str(row[0])
            row = conn.exec_driver_sql("PRAGMA cache_size;").fetchone()
            actual["cache_size"] = "" if row is None else str(row[0])

        for pragma, expected in _HARDCODED_PRAGMAS:
            got = actual[pragma].lower()
            want = expected.lower()
            if pragma == "synchronous":
                # SQLite reports synchronous as integer (0|1|2|3); NORMAL == 1
                if got not in {"1", "normal"}:
                    raise StoragePragmaMismatchError(f"synchronous expected NORMAL/1, got {got}")
            elif got != want:
                raise StoragePragmaMismatchError(f"PRAGMA {pragma} expected {want}, got {got}")

        expected_cache = -self._settings.cache_size_mb * 1000
        if int(actual["cache_size"]) != expected_cache:
            raise StoragePragmaMismatchError(
                f"cache_size expected {expected_cache}, got {actual['cache_size']}"
            )

        return actual


__all__ = [
    "DBEngine",
    "StorageSettings",
    "StoragePragmaMismatchError",
]
