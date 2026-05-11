# Task 2: SQLite DB Engine + PRAGMA Configuration (P1-2)

## Context

Read: overview.md (esp. "WAL + busy_timeout are hard-coded" key decision)

This task implements the **DB engine factory** that produces a configured SQLAlchemy 2.0 Core `Engine`. Per SD §3.5, four PRAGMAs are hard-coded best practice and one (`cache_size`) is user-configurable. **The PRAGMA application MUST be verified per-connection** — it is not enough to send the statement; we must read it back to confirm it took effect.

**Plan ref:** P1-2
**SD refs:** §3.5

The SD prescribes this exact configuration:

```python
def configure_connection(conn, settings: StorageSettings):
    # Hard-coded best practice
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    # Configurable
    conn.execute(f"PRAGMA cache_size=-{settings.cache_size_mb * 1000};")
```

## Files

- Create: `src/secondsight/storage/db_engine.py` — `DBEngine`, `StorageSettings`
- Create: `src/secondsight/storage/_pragma.py` — pragma list + verification helper (private)
- Create: `tests/storage/test_db_engine.py`

## Public Contract

```python
@dataclass(frozen=True)
class StorageSettings:
    cache_size_mb: int = 64

    def __post_init__(self) -> None:
        # validate cache_size_mb > 0; layered config later (defaults → file → env)
        ...

class DBEngine:
    """SQLAlchemy Core engine wrapper for a per-project intelligence.db.

    Applies hard-coded PRAGMAs on every new connection via 'connect' event.
    Verifies WAL mode is actually active after first connection or raises.
    """

    def __init__(self, db_path: Path, settings: StorageSettings | None = None) -> None: ...

    @property
    def engine(self) -> sqlalchemy.Engine: ...

    def dispose(self) -> None:
        """Close the underlying engine. Idempotent."""

    def verify_pragmas(self) -> dict[str, str]:
        """Open a connection, read back every PRAGMA, return the actual values.
        Raises StoragePragmaMismatchError if any hard-coded PRAGMA did not stick.
        """
```

## Death Test Requirements

1. **WAL mode silently rejected.** Mock `sqlite3.Connection.execute` so `PRAGMA journal_mode=WAL` returns rows reporting `delete` (the rejection signal). `DBEngine` constructor or first `engine.connect()` must raise — NOT silently fall back to rollback journal.
2. **Settings struct mutated after construction.** `StorageSettings` is `frozen=True`. Attempting to set `settings.cache_size_mb = 0` must raise `dataclasses.FrozenInstanceError`. `cache_size_mb <= 0` must fail validation in `__post_init__`.
3. **Connection without PRAGMA application.** Acquire a connection through SQLAlchemy NotConnect path (e.g., raw `connect(db_url)` bypassing event listener) and assert the listener path is the only legal entry. (Catches a future regression where someone adds a second connection helper that skips the listener.)
4. **`verify_pragmas` reports mismatch but caller ignores it.** The method MUST raise on mismatch — returning a dict and trusting the caller to check is a silent-failure pattern. Test asserts that calling on a hypothetically misconfigured engine raises, never returns.
5. **Engine reuse after `dispose()`.** Calling `engine` after `dispose()` must raise an explicit error, not return a stale engine.

## Unit Test Requirements

- All 5 PRAGMAs round-trip via `verify_pragmas()` on a fresh tmp-path DB
- `cache_size_mb=128` produces `PRAGMA cache_size=-128000` exactly
- Concurrent connections from threads each get the PRAGMAs applied (test with 4 threads × 10 connects)
- DB file is created if missing; parent directory is created if missing
- Dispose is idempotent (call twice, second is no-op)

## Implementation Steps

- [ ] Step 1: STEP 0 prerequisite questions in scar draft
- [ ] Step 2: Write death tests
- [ ] Step 3: Run death tests — red
- [ ] Step 4: Write unit tests
- [ ] Step 5: Run unit tests — red
- [ ] Step 6: Implement `StorageSettings` (frozen dataclass with validation)
- [ ] Step 7: Implement `DBEngine` using SQLAlchemy `event.listen(engine, "connect", ...)`
- [ ] Step 8: Implement `verify_pragmas` reading-back-and-asserting
- [ ] Step 9: Run all tests — green
- [ ] Step 10: Write scar report
- [ ] Step 11: Self-iteration

## Expected Scar Report Items

- Silent failure: SQLAlchemy `Engine` connection pool reuses connections; if listener registration fails silently, second-onward connections lack PRAGMAs
- Silent failure: filesystems that don't support shared-memory (some tmpfs / NFS) silently downgrade WAL — `verify_pragmas` catches this only on connect, not on later degradation
- Assumption: SQLAlchemy 2.0 `event.listen` ordering vs lazy connection establishment
- Configuration drift: layered config (defaults → file → env → CLI) not yet implemented; today only constructor argument

## Acceptance Criteria

- All death tests pass
- All unit tests pass
- `verify_pragmas()` is called at least once during `DBEngine.__init__` so misconfiguration fails on construction, not on first INSERT
- mypy clean
- Scar report items addressed
