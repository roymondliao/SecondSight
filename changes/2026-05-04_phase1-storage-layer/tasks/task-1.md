# Task 1: Raw Trace Store (P1-1)

## Context

Read: overview.md (esp. "Filesystem write is the failure barrier" key decision)

This task implements the **filesystem-first durability layer**. Per SD §3.1, the filesystem is source of truth — every event lands here as a self-contained JSON file before the DB index is updated. If the DB is wiped or corrupted, raw traces alone must be sufficient to rebuild every event.

**Plan ref:** P1-1
**SD refs:** §3.1, §3.7.5 (event JSON shape), §3.9 (pipeline ordering)

**Layout (per SD §3.3):**
```
~/.secondsight/projects/{project_id}/sessions/{session_id}/events/
  {timestamp}_{event_type}.json
```

`{timestamp}` MUST be lexicographically sortable (ISO-8601 with millisecond precision in UTC). On collision (two events at the same millisecond — possible for fast tool calls), append a monotonic counter `_NNN` to disambiguate. The PoC at `src/secondsight/poc/storage.py:_event_filename` uses a per-session counter — do NOT inherit its design as-is, but cross-reference it for shape.

## Files

- Create: `src/secondsight/event.py` — production `Event` Pydantic model (lift from `poc/event_schema.py`, drop POC-only fields, keep schema aligned with SD §3.7.5)
- Create: `src/secondsight/storage/__init__.py`
- Create: `src/secondsight/storage/raw_trace_store.py` — `RawTraceStore` class
- Create: `tests/__init__.py` (if missing for production tests, separate from `tests/poc/`)
- Create: `tests/storage/__init__.py`
- Create: `tests/storage/test_raw_trace_store.py`
- Update: `src/secondsight/__init__.py` — re-export key types

## Public Contract

```python
class RawTraceStore:
    def __init__(self, project_root: Path) -> None: ...

    async def write(self, event: Event) -> Path:
        """Write an event JSON to disk. Returns the absolute path written.

        Raises:
          OSError: filesystem unavailable / permission denied (do NOT swallow)
          ValueError: event missing required fields for path computation
        """

    def event_path(self, event: Event) -> Path:
        """Compute the path WITHOUT writing. Pure, deterministic given event."""

    async def read(self, path: Path) -> Event:
        """Read a previously-written trace back. Used by backfill."""

    async def iter_session(self, session_id: str) -> AsyncIterator[Path]:
        """Yield every event file path for a session, sorted lexicographically."""
```

## Death Test Requirements

These tests MUST be written and observed failing BEFORE any production code is written:

1. **Sub-millisecond collision swallows events.** Submit 100 events with timestamps colliding to the same millisecond and the same `event_type`. Assert that 100 distinct files exist and `read()` returns each event correctly. (Catches naive `{timestamp}_{event_type}.json` overwriting.)
2. **Partial write leaves corrupt JSON on disk.** Inject `OSError` mid-write (mock `aiofiles` write to fail after first chunk). Assert no half-file remains — `event_path()` either returns a fully-readable JSON or no file at all (atomic via tmp + rename).
3. **Reading must reject silent corruption.** Manually truncate a written file to 5 bytes. `read()` must raise a typed error, not return `None` or a partial dict.
4. **Path traversal injection.** Construct an `Event` whose `session_id = "../../etc/passwd"`. `event_path()` must reject it (not write outside the project root). Same for `event_type` containing `/` or `..`.
5. **Filesystem-first ordering breakable from outside.** Verify that no other phase-1 module writes a trace path on its own — `RawTraceStore` is the **only** writer. (Static check: grep for direct `open(path, "w")` in `storage/*.py` outside this module.)

## Unit Test Requirements

After death tests are red:

- Round-trip: every `event_type` from SD §3.7.2 writes and reads back unchanged
- Path determinism: `event_path()` for the same event returns the same path across processes
- Concurrent writes from two `asyncio.gather` calls produce two distinct files
- Empty-session `iter_session()` returns empty iterator (not raise)
- Lexicographic ordering of `iter_session()` matches insertion order when timestamps are monotonic

## Implementation Steps

- [ ] Step 1: STEP 0 — answer the four prerequisite questions in scar report draft
- [ ] Step 2: Write death tests (5 cases above)
- [ ] Step 3: Run death tests — verify red
- [ ] Step 4: Write unit tests
- [ ] Step 5: Run unit tests — verify red
- [ ] Step 6: Implement `Event` model (lift + trim from `poc/event_schema.py`)
- [ ] Step 7: Implement `RawTraceStore` with atomic write (tmp file + `os.replace`)
- [ ] Step 8: Run all tests — green
- [ ] Step 9: Write scar report
- [ ] Step 10: Self-iteration (Level 1) — fix task-scope items, defer feature-level
- [ ] Step 11: Re-run tests — no regression

## Expected Scar Report Items

- Potential silent failure: `os.replace` is atomic on POSIX but NOT cross-device — if `/tmp` is a different filesystem, this breaks
- Potential silent failure: directory creation race when two events for the same new session arrive concurrently
- Assumption to verify: `aiofiles` is acceptable as a new dependency (or do we wrap sync I/O in `asyncio.to_thread`?)
- Potential shortcut: storing the event as `event.model_dump_json()` without versioning — schema-evolution flag deferred
- Boundary issue: how does the store behave when project_root does not yet exist? Auto-create vs raise?

## Acceptance Criteria

- All death tests pass
- All unit tests pass
- `mypy` clean (project's pre-commit config)
- Scar report contains at least the items above with explicit `resolved_items` or `deferred_to_feature_iteration` flags
- Public contract docstrings match the implementation
- No imports from `secondsight.poc.*`
