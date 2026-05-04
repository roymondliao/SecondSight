"""SecondSight storage layer — Phase 1.

Public surface:
    RawTraceStore     — filesystem-first durability (P1-1)
    DBEngine          — SQLAlchemy engine + PRAGMA (P1-2)
    EventsRepository  — events table CRUD (P1-3)
    SyncLog           — JSONL append-only failure log
"""

from __future__ import annotations

from secondsight.storage.db_engine import (
    DBEngine,
    StoragePragmaMismatchError,
    StorageSettings,
)
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import (
    RawTraceCorruptionError,
    RawTraceStore,
    UnsafePathError,
)

__all__ = [
    "DBEngine",
    "EventsRepository",
    "RawTraceCorruptionError",
    "RawTraceStore",
    "StoragePragmaMismatchError",
    "StorageSettings",
    "UnsafePathError",
]
