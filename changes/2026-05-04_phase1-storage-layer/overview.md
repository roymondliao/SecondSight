# Overview: Phase 1 — Storage Layer

## Goal

Build the production-grade storage foundation that turns Phase 0's monolithic `DualLayerStorage` PoC into four single-responsibility modules: a filesystem-first raw trace store, a configured SQLite engine, an events repository over SQLAlchemy Core, and an observation pipeline that orchestrates them with explicit failure semantics.

## Architecture

The storage layer enforces **filesystem-first durability** (SD §3.1): the filesystem is source of truth, SQLite is a derived index. This inverts the usual relational mental model and underwrites the Phase 1 reliability claim — DB corruption, schema migration error, or transient lock contention cannot lose user data.

```
┌─────────────────────────────────────────────────────────────────┐
│                   ObservationPipeline (P1-4)                    │
│  ingest(event):                                                 │
│    1. await raw_trace_store.write(event)   ← durability boundary│
│    2. try: await event_repo.insert(event)                       │
│    3. except: sync_log.record_failure(event.id, error)          │
└─────────────────────────────────────────────────────────────────┘
       │                                │
       ▼                                ▼
┌──────────────────────┐    ┌────────────────────────────────────┐
│ RawTraceStore (P1-1) │    │ EventsRepository (P1-3)            │
│  filesystem JSON     │    │  SQLAlchemy Core, single table     │
│  {ts}_{type}.json    │    │  INSERT / get_session_events / …   │
└──────────────────────┘    └────────────────┬───────────────────┘
                                             │
                                             ▼
                            ┌────────────────────────────────────┐
                            │ DBEngine (P1-2)                    │
                            │  SQLite + WAL + PRAGMA on connect  │
                            └────────────────────────────────────┘
```

## Tech Stack

- Python 3.14, uv-managed
- SQLAlchemy 2.0 Core (no ORM — Repository pattern over `Table`/`select`/`insert`)
- `aiofiles` (async FS I/O — to be added if not present) or asyncio thread-pool wrap
- `loguru` for structured logging, `pendulum` for timestamps
- `pytest` + `pytest-asyncio` for tests
- Per-project DB layout: `~/.secondsight/projects/{project_id}/intelligence.db`

## Key Decisions

- **Split the PoC monolith.** `DualLayerStorage` (poc/storage.py, ~700 LOC) becomes four modules with clear contracts. Phase 1 does **not** import from `secondsight.poc.*` — the PoC stays as a frozen reference.
- **WAL + busy_timeout are hard-coded.** Per SD §3.5, only `cache_size_mb` is user-configurable. PRAGMA application is verified per-connection, not assumed from connection-string.
- **Filesystem write is the failure barrier.** If FS write fails, the pipeline raises and the caller sees it. If DB INSERT fails, the pipeline records to sync log and **swallows** the error — DB is best-effort. This asymmetry is the durability contract.
- **Events table is single-row per event.** No PreToolUse/PostToolUse merging at write time (SD §3.7.3); pairing happens at analysis time. Idempotent INSERT keyed by `id` (deterministic UUID) so retries are safe.
- **No connection pool yet.** Per-project DB + low write throughput (0.5–2 events/sec, SD §3.2) → single connection per `DBEngine` instance with `check_same_thread=False` only when an explicit thread-pool is involved. Async I/O wraps sync sqlite3 via `asyncio.to_thread`.
- **Sync log format**: append-only JSONL at `{project_dir}/sync_failures.jsonl`. One line per failure: `{event_id, timestamp, raw_trace_path, error_class, error_message}`. P1-13 backfill consumes this.

## Death Cases Summary

These are the silent-failure paths Phase 1 must instrument:

1. **DB write succeeds but FS write was lost (or order reversed).** Detection: every `EventsRepository.insert` test asserts the corresponding raw trace file exists on disk first.
2. **PRAGMA `journal_mode=WAL` silently rejected** (e.g., DB on network FS). Detection: `DBEngine` reads back `PRAGMA journal_mode` after applying and raises if not `wal`.
3. **DB INSERT failure swallowed without sync log.** Detection: pipeline death test injects DB failure, asserts sync log line exists with the right `event_id` and `raw_trace_path`.
4. **Filename collisions overwriting events.** Two events with same `{timestamp}_{event_type}` (sub-millisecond). Detection: RawTraceStore must produce unique paths; collision test creates 100 events with duplicate timestamps and asserts no overwrite.
5. **Sub-agent nesting state lost on restart.** Detection: ingest events with depth>0, restart pipeline, query — depth/sub_agent_id columns must round-trip exactly.
6. **`segment_index` recomputed inconsistently across restarts** (P1-3 read path issue). Detection: insert events out of order, query `MAX(segment_index)` per session — must equal the highest user_prompt count seen.

## File Map

### Source — production
- `src/secondsight/storage/__init__.py` — package init, public exports
- `src/secondsight/storage/raw_trace_store.py` — RawTraceStore (P1-1)
- `src/secondsight/storage/db_engine.py` — DBEngine factory (P1-2)
- `src/secondsight/storage/events_repository.py` — EventsRepository + Table (P1-3)
- `src/secondsight/storage/sync_log.py` — JSONL append-only sync log
- `src/secondsight/observation/__init__.py`
- `src/secondsight/observation/pipeline.py` — ObservationPipeline (P1-4)
- `src/secondsight/event.py` — Production Event model (Pydantic, lifted from PoC)

### Tests
- `tests/storage/test_raw_trace_store.py`
- `tests/storage/test_db_engine.py`
- `tests/storage/test_events_repository.py`
- `tests/observation/test_pipeline.py`
- `tests/conftest.py` — shared fixtures (tmp_path-based project dir, sample events)

### Design notes (this change)
- `changes/2026-05-04_phase1-storage-layer/storage-layer-design.md` — design decisions written during implementation

## Non-Goals (Phase 1 Storage Layer)

- API server, hooks endpoints (P1-5..P1-8 — separate change)
- Adapters and hook scripts (P1-9..P1-11 — separate change)
- CLI scaffold, sync subcommand (P1-12, P1-13 — separate change)
- Behavior flags / directives tables (Phase 2)
- Connection pooling / multi-process write coordination
- Cross-project queries against `registry.db`
