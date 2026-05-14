"""SecondSight storage layer — Phase 1 + Phase 2.

Public surface:
    RawTraceStore               — filesystem-first durability (P1-1)
    DBEngine                    — SQLAlchemy engine + PRAGMA (P1-2)
    EventsRepository            — events table CRUD (P1-3)
    SyncLog                     — JSONL append-only failure log
    BehaviorFlagsRepository     — behavior_flags table CRUD (GUR-100)
    DirectivesRepository        — directives table + lifecycle (GUR-100)
    AnalysisRunsRepository      — analysis_runs table CRUD (GUR-102)
    AnalysisOutputsRepository   — analysis_outputs table CRUD (Task 6)

Table registration invariant:
    ALL table modules must be imported here (directly or via their repository)
    so that `metadata.create_all()` always picks up every table. SQLAlchemy
    registers a table with shared MetaData at `sa.Table(name, metadata, ...)`
    call time — which happens at import time. If a table module is never
    imported before `metadata.create_all()`, that table is silently skipped.
    See: tests/storage/test_table_registration.py for the regression guard.
"""

from __future__ import annotations

# CRITICAL: ALL table modules must be imported here to register their tables
# with shared metadata. metadata.create_all() relies on import-time side effects.
# Do NOT remove these imports even if linters flag them as "unused" — they are
# the registration mechanism. Add noqa: F401 if needed.
from secondsight.storage.analysis_outputs_repository import (
    AnalysisOutputsRepository,  # also imports analysis_outputs_table → registers table
)
from secondsight.storage.analysis_runs_repository import (
    AnalysisRunsRepository,  # also imports analysis_runs_table → registers table
)
from secondsight.storage.behavior_flags_repository import (
    BehaviorFlagsRepository,  # also imports behavior_flags_table + session_reports_table → registers both
)
from secondsight.storage.db_engine import (
    DBEngine,
    StoragePragmaMismatchError,
    StorageSettings,
)
from secondsight.storage.directives_repository import (
    DirectivesRepository,  # also imports directives_table → registers table
)
from secondsight.storage.events_repository import (
    EventsRepository,  # also imports events_table (the metadata owner) → defines metadata
)
from secondsight.storage.ingress_record import IngressRecord
from secondsight.storage.raw_ingress_store import RawIngressStore
from secondsight.storage.raw_trace_store import (
    RawTraceCorruptionError,
    RawTraceStore,
    UnsafePathError,
)

__all__ = [
    "AnalysisOutputsRepository",
    "AnalysisRunsRepository",
    "BehaviorFlagsRepository",
    "DBEngine",
    "DirectivesRepository",
    "EventsRepository",
    "IngressRecord",
    "RawTraceCorruptionError",
    "RawIngressStore",
    "RawTraceStore",
    "StoragePragmaMismatchError",
    "StorageSettings",
    "UnsafePathError",
]
