# Task 4: Observation Pipeline (P1-4)

## Context

Read: overview.md (esp. "Filesystem write is the failure barrier")
Depends on: task-1 (RawTraceStore), task-3 (EventsRepository)

The pipeline is a 20-line orchestrator that encodes the **durability contract** in code:

> Filesystem write is the failure barrier. If FS fails, raise. If DB INSERT fails, log to sync log and continue.

This is the highest-risk module in Phase 1 because the asymmetric error handling is exactly the place where a programmer's instinct ("wrap everything in try/except, swallow, log") corrupts the design.

**Plan ref:** P1-4
**SD refs:** §3.4 (DB sync mechanism), §3.9 (data flow)

## Files

- Create: `src/secondsight/observation/__init__.py`
- Create: `src/secondsight/observation/pipeline.py`
- Create: `src/secondsight/storage/sync_log.py`
- Create: `tests/observation/__init__.py`
- Create: `tests/observation/test_pipeline.py`

## Public Contract

```python
class SyncLog:
    """Append-only JSONL log of DB INSERT failures for backfill (P1-13)."""

    def __init__(self, path: Path) -> None: ...

    def record_failure(
        self,
        event_id: str,
        raw_trace_path: Path,
        error: BaseException,
    ) -> None:
        """Append one JSON line. Sync write (small, infrequent)."""

    def iter_pending(self) -> Iterator[SyncLogEntry]: ...


class ObservationPipeline:
    def __init__(
        self,
        raw_trace_store: RawTraceStore,
        events_repository: EventsRepository,
        sync_log: SyncLog,
    ) -> None: ...

    async def ingest(self, event: Event) -> None:
        """
        1. await raw_trace_store.write(event)   ← raises on failure
        2. try: events_repository.insert(event)
           except Exception as e: sync_log.record_failure(...); log.warning(...)
        Returns None on success or partial-success (FS+log written).
        Raises ONLY when filesystem write fails.
        """
```

## Death Test Requirements

1. **DB failure swallowed without sync log entry.** Inject `EventsRepository.insert` to raise `OperationalError`. After `await ingest(event)` returns, assert sync log file contains exactly one entry with `event_id`, `raw_trace_path`, and the error class. (The most important death test — this is THE silent-failure path.)
2. **FS failure leaks DB writes.** Inject `RawTraceStore.write` to raise. Assert no DB row exists for that event afterward. (The pipeline must NOT have called `events_repository.insert` because FS came first.)
3. **Sync log itself fails.** Mock `SyncLog.record_failure` to raise. Pipeline must surface this as a CRITICAL log line and re-raise — losing both the DB row AND the sync log silently is unacceptable. (The "everything broke, give up loudly" path must exist.)
4. **Out-of-order arrival.** Two `ingest()` coroutines for the same session run in `asyncio.gather`. Both must succeed; both raw traces must be on disk; both DB rows must exist (UNIQUE on `(session_id, sequence_number)` is upstream's job, but pipeline must not introduce its own ordering bugs).
5. **Crash between FS write and DB INSERT.** Simulate by raising `KeyboardInterrupt` in `events_repository.insert`. Raw trace must remain on disk. Backfill (P1-13) will recover it. Pipeline does not "rollback" the FS write — that would defeat filesystem-first.
6. **`record_failure` writes incomplete JSONL line if process killed mid-write.** Sync log writes must be atomic per line (write to tmp + rename, OR use `os.write` of a single buffer). Test by truncating mid-line and verifying `iter_pending` skips/raises rather than parsing garbage.

## Unit Test Requirements

- Happy path: ingest 100 events → 100 raw traces on disk + 100 DB rows + 0 sync log entries
- DB-down path: ingest 50 events with DB raising on every insert → 50 raw traces + 0 DB rows + 50 sync log entries
- Mixed: 25 successes + 25 DB failures interleaved → 50 raw traces, 25 DB rows, 25 sync log entries with correct event_ids
- `iter_pending` returns entries in append order
- Concurrent `ingest` from `asyncio.gather` of size 50 — no missed events, no duplicate sync entries

## Implementation Steps

- [ ] Step 1: STEP 0 prerequisite questions
- [ ] Step 2: Death tests (most critical of the four tasks)
- [ ] Step 3: Death tests red
- [ ] Step 4: Unit tests
- [ ] Step 5: Unit tests red
- [ ] Step 6: Implement `SyncLog` (atomic append-line)
- [ ] Step 7: Implement `ObservationPipeline.ingest`:
    - `await raw_trace_store.write(event)` — let exceptions propagate
    - `try: events_repository.insert(event)`
    - `except Exception as e:` → `sync_log.record_failure(...)` and `log.warning(...)`
    - If sync_log itself raises, log CRITICAL and re-raise
- [ ] Step 8: Run all tests — green
- [ ] Step 9: Scar report
- [ ] Step 10: Self-iteration (Level 1)

## Expected Scar Report Items

- Silent failure: `Exception` is too broad — catching `BaseException` would also swallow `KeyboardInterrupt`. Must catch only DB-typed errors (SQLAlchemy `DBAPIError`/`OperationalError`)
- Silent failure: sync log path can become huge if DB is down for hours; no rotation in Phase 1 (deferred to ops)
- Assumption: `events_repository.insert` is sync (SQLAlchemy 2.0 sync engine wrapped in `asyncio.to_thread`?) — async vs sync boundary needs to be explicit
- Boundary: pipeline does not retry DB INSERT — it relies on backfill. Document this so future readers don't add retry-with-backoff and break the durability semantic
- Cross-task concern: `SyncLog` lives under `storage/` but is consumed by `observation/` — module boundary is debatable

## Acceptance Criteria

- All death tests pass — especially #1 (DB failure → sync log) and #3 (sync log failure → CRITICAL + re-raise)
- All unit tests pass
- mypy clean
- Pipeline source file is < 100 LOC (this is a coordination class, not a feature factory)
- The asymmetric error contract is documented in the class docstring
- Scar report explicitly notes whether the broad `except` was tightened in Level 1 self-iteration
