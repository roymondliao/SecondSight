"""SecondSight storage layer — Phase 1 + Phase 2.

Public surface:
    RawTraceStore             — filesystem-first durability (P1-1)
    DBEngine                  — SQLAlchemy engine + PRAGMA (P1-2)
    EventsRepository          — events table CRUD (P1-3)
    SyncLog                   — JSONL append-only failure log
    BehaviorFlagsRepository   — behavior_flags table CRUD (GUR-100)
    DirectivesRepository      — directives table + lifecycle (GUR-100)
"""

from __future__ import annotations

from secondsight.storage.behavior_flags_repository import (
    BehaviorFlagsRepository,
)
from secondsight.storage.db_engine import (
    DBEngine,
    StoragePragmaMismatchError,
    StorageSettings,
)
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import (
    RawTraceCorruptionError,
    RawTraceStore,
    UnsafePathError,
)

__all__ = [
    "BehaviorFlagsRepository",
    "DBEngine",
    "DirectivesRepository",
    "EventsRepository",
    "RawTraceCorruptionError",
    "RawTraceStore",
    "StoragePragmaMismatchError",
    "StorageSettings",
    "UnsafePathError",
]
