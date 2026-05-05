# Task 3: MH-3 — Server-down fallback + sync archive (G1-α) + idempotency

## Context

Read: `overview.md` for full architecture and decisions.

This task adds `TestMH3FallbackArchive` to `tests/integration/test_phase1_e2e.py`.

**Critical scope decision (G1-α, board-confirmed)**: Phase 1 `secondsight sync` ARCHIVES `fallback_events.jsonl` to a timestamped `.bak` but does NOT re-INSERT the events into the DB. Path C (replay) is documented carry-forward (see `src/secondsight/storage/filesystem_backfill.py:16-22`). DO NOT write any assertion expecting fallback events to appear in the DB after sync — that would test behavior that does not exist.

The fixtures used (already exist):
- `hook_script`, `run_hook`, `build_env`, `FALLBACK_FILENAME` from `tests/scripts/conftest.py`
- `real_secondsight_server` (only used for the "start server" portion)

For invoking `secondsight sync` programmatically, the cleanest approach is `typer.testing.CliRunner` against `secondsight.cli.sync.app`. Subprocess is an alternative but adds startup cost; CliRunner exercises the same code path.

The archive function is `secondsight.storage.filesystem_backfill.archive_fallback_events(fallback_path) -> FallbackArchiveReport`. The CLI `secondsight sync` calls it after the per-project backfill loop.

## Files

- Modify: `tests/integration/test_phase1_e2e.py` — add `TestMH3FallbackArchive` class
- Test: same file

## Death Test Requirements

- **DT-3.1** — After 5 hooks fire against a dead port, `fallback_events.jsonl` has exactly 5 lines (no truncation, no duplicates).
- **DT-3.2** — After running `secondsight sync`, fallback file is renamed to `fallback_events.<timestamp>.bak` with all 5 lines preserved AND the original is gone or empty AND zero DB rows for those events (G1-α: replay is NOT happening).
- **DT-3.3 (idempotency)** — Re-running `secondsight sync` immediately after a successful run must NOT create a second `.bak` file (no double-archive of an already-empty fallback). Failure message: "sync double-archived an empty fallback".

## Implementation Steps

- [ ] Step 1: Read `src/secondsight/cli/sync.py` (already read in research) to confirm CLI invocation shape: `runner.invoke(sync_app, ['--home', str(home)])`.
- [ ] Step 2: Read `src/secondsight/storage/filesystem_backfill.py::archive_fallback_events` to confirm the timestamp suffix format on `.bak` files.
- [ ] Step 3: Write death tests DT-3.1, DT-3.2, DT-3.3. Run — verify red.
- [ ] Step 4: Implement against existing code — no production changes expected. Run — confirm green.
- [ ] Step 5: Stress test: 10× loop. Tighten any race-condition assertions (e.g. file-system glob).
- [ ] Step 6: Write scar report. Commit.

## MH-3 specifics

```python
class TestMH3FallbackArchive:
    """MH-3: Server-down fallback file is archived (G1-alpha)."""

    def test_mh3_dead_port_writes_five_fallback_lines(
        self, tmp_path: Path
    ) -> None:
        # DT-3.1: 5 sequential hooks against port=1 (dead) → 5 valid JSONL lines.
        ...

    def test_mh3_sync_archives_fallback_no_db_replay(
        self, tmp_path: Path, real_secondsight_server
    ) -> None:
        # DT-3.2: After 5 fallback writes, run secondsight sync.
        # Use CliRunner against the actual secondsight.cli.sync.app.
        # Pass --home pointing at the same SECONDSIGHT_HOME used by the dead-port hooks.
        # Assert: fallback_events.<timestamp>.bak exists with 5 lines.
        # Assert: fallback_events.jsonl gone or empty.
        # Assert: DB has zero rows for the fallback event_ids.
        # G1-α: this is the contract; do NOT assert events are in DB.
        ...

    def test_mh3_sync_idempotent_on_empty_fallback(
        self, tmp_path: Path
    ) -> None:
        # DT-3.3: After a successful first sync (archive complete),
        # run sync again immediately. Count .bak files in home dir.
        # Must remain at 1 (or whatever was created in the first run).
        # Failure message: "sync double-archived an empty fallback".
        ...
```

## Expected Scar Report Items

- Potential shortcut: re-running sync expecting 0 .bak files because "the file is gone" — but `archive_fallback_events` may handle missing-file differently than empty-file. Read the implementation to be sure.
- Potential shortcut: writing to `home / FALLBACK_FILENAME` directly via Python `open(...)` for fixture setup, instead of going through bash hooks — ACCEPTABLE here because we are testing sync, not the hook→fallback path. Document the deviation in a test docstring.
- Assumption to verify: `archive_fallback_events` is exported from `secondsight.storage.filesystem_backfill`. (Read confirmed.)
- Assumption to verify: CliRunner can invoke `secondsight.cli.sync.app` directly (i.e. it's a Typer app). (Read confirmed.)

## Acceptance Criteria

- Covers: "Degradation - server down, hook activates fallback path"
- Covers: "Silent failure - sync archives fallback file before INSERTs durable" (DT-3.2 covers post-archive integrity; the "before durable" mid-write race is harder to test — document as a known gap if the archive function lacks a way to inject a mid-write fault)
- Phase 1 contract: Path C (replay into DB) is NOT asserted; tracked as P1-13 carry-forward.
