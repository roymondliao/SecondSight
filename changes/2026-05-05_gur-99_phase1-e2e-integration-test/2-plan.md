# 2-plan: GUR-99 Phase 1 End-to-End Integration Test

> Prerequisites: `1-kickoff.md`, `problem-autopsy.md`, `2-pre-thinking.md`.
> Gap resolutions confirmed by board at 2026-05-05T06:26:21Z:
> **G1=G1-α**, **G2** corrected, **G3** corrected, **G4=G4-β**, **G5=G5-β**.

## Goal

Add a seam-level test layer at `tests/integration/test_phase1_e2e.py` that exercises the user-visible install-and-run lifecycle end-to-end. Five must-have scenarios (MH-1..MH-5) close gaps between existing component-level tests.

## Architecture

- **One new test file**: `tests/integration/test_phase1_e2e.py`
- **Direct import** of fixtures from `tests/scripts/conftest.py` (no fixture lifting; smallest blast radius)
- **No new production code** — this is a test-only change. If a test cannot be made green without changing production code, that change must be split into a separate prerequisite task with its own samsara cycle.
- **Fail-loud prerequisite gate** at module level: missing `bash`/`curl`/`jq` produces `pytest.skip` with a named tool, never a silent green pass.

## Tech Stack

- pytest + uvicorn + FastAPI (existing)
- subprocess + sockets (live e2e, no in-process emulation)
- typer.testing.CliRunner (for CLI smoke in MH-5)

## Key Decisions (from research + pre-thinking)

- **Test bash, not Python** (kickoff Step 0 commitment 1): every MH that names "hook" must invoke the `.sh` file via subprocess. Using `client.post()` against FastAPI TestClient is forbidden in this layer — it bypasses the seams under test.
- **G1-α**: MH-3 tests archive-only behavior. `secondsight sync` re-INSERT-from-fallback is **not** Phase 1 contract; the assertion stops at "fallback file moved to .bak".
- **G2 correction**: MH-2 sequence interleaves `user-prompt` events to actually exercise `segment_index` increment.
- **G3 correction**: MH-2 uses explicit `sub_agent_start`/`sub_agent_end` events with `data["sub_agent_id"]`. Adds a stack-mismatch death case.
- **G4-β**: MH-4 prints latency histogram to stderr; no on-disk artifact.
- **MH-4 latency budget = p95 ≤ 50ms** (CI-stable proxy for SD §3.9.1's 7ms theoretical). MH-4 docstring includes the death condition: remove when production-side latency metric exists.
- **CliRunner over subprocess for MH-5 init/sync/status**: `secondsight init` and `sync` are pure Python and CliRunner exercises the same code path with less flake. **Subprocess required for `serve --daemon`** because daemonization forks the process.

## Death Cases (top 3 silent-failure paths this PR closes)

1. **Pre-tool-use.sh URL drift** — if a future change makes the bash script POST to `/hook/pre_tool_use` (underscore) while the FastAPI route stays `/hook/pre-tool-use`, every event silently falls back to JSONL. MH-1 catches via `assert no fallback line written` after a successful hook fire against a live server.
2. **`segment_index` stuck at 0** — if `tracker.bind()` ever returns the cached value without applying the `USER_PROMPT` increment, every event in a session shares `segment_index=0`. MH-2 catches via "after two user-prompt events, observe distinct segment_index values 1 and 2 in DB."
3. **Sync archives fallback before commit** — if `archive_fallback_events()` ever moves the file before the .bak path is durable, a crash mid-archive loses pending work. MH-3 catches via "fallback exists and contains N lines pre-sync; post-sync .bak exists with same N lines and original is gone-or-empty; both observable atomically."

## File Map

- `tests/integration/__init__.py` — new (empty package marker)
- `tests/integration/test_phase1_e2e.py` — new (5 test classes, one per MH)
- `tests/integration/_prereqs.py` — new (PATH-tool detection helper, named-skip)
- `pyproject.toml` — modify (`[tool.pytest.ini_options]` may need `testpaths` update if not already inclusive)

No production code changes. Confirmed by gap-resolution G1-α.

## Test Inventory (per-MH)

### MH-1 — Single-event hook → server → DB row + raw trace

- 1 happy path (`test_mh1_single_event_lands_in_db`)
- 1 death path: server URL drift (`test_mh1_no_fallback_when_server_accepts`)
- 1 evidence chain: assert `event_id`, `session_id`, `segment_index=0`, `sub_agent_id is null`, `depth=0`

### MH-2 — Multi-event session sequence

- 1 happy path: 7-event sequence (`session-start → user-prompt → pre/post → user-prompt → pre/post → session-end`) — assert segment_index goes 0→1→1→1→2→2→2
- 1 sub-agent nesting: 5-event sequence with `sub_agent_start → user-prompt → sub_agent_end` — assert `depth` toggles 0→1→0
- 1 death path (stack mismatch): `sub_agent_end` with id that doesn't match top-of-stack → server returns 4xx, no DB row inserted

### MH-3 — Server-down → fallback → sync archive → idempotent

- 1 happy path: hook against dead port × 5 → 5 lines in `fallback_events.jsonl` → start server → run sync → assert `.bak` exists with 5 lines, original gone, no DB rows from fallback
- 1 idempotent re-run: re-run sync, assert no new `.bak` created, no error
- 1 death path: re-run sync **does not double-archive** (no second `.bak.<timestamp>` from an empty original)

### MH-4 — Hook latency wall-clock

- N=50 sequential hooks → record p50, p95, p99 → print histogram to stderr → assert `p95 ≤ 50ms`
- 1 invariant: histogram not empty (at least 50 valid measurements; reject if subprocess timed out on any iteration)
- Documented as time-bounded (death condition in test docstring): remove when production latency metric exists

### MH-5 — CLI lifecycle composition

- Sequence: `init --dry-run` → `init` (real) → assert hook scripts in `~/.claude/hooks/`, settings.json patched → start `serve --daemon` (subprocess) → poll for PID file + port readiness (timeout 5s) → fire 1 hook → assert DB row → `serve --stop` → assert PID file gone, port no longer bound → `status --format json` → assert "not running" + correct event count
- 1 death path: `serve --daemon` PID file present but port not bound → status must report "not running" (or "stale-pid"), never "running"

## Step 0 Commitments (carried forward from kickoff)

1. **No Python emulation of bash hooks.** Every "hook fires" assertion in MH-1, MH-2, MH-3 invokes `subprocess.run(['bash', script])`. Failure to comply is a review-blocker.
2. **Skip-with-named-tool when prereqs missing** — never silently green-pass.
3. **Silent failure surface this PR closes**: enumerated in Death Cases above.
4. **What lives one year from now**: MH-1, MH-2, MH-3, MH-5 are durable. MH-4 is time-bounded with explicit removal condition in docstring.

## Risks

- **Flake risk**: subprocess + uvicorn + bash + curl. Mitigations: kernel-assigned ports (already in real fixture), generous timeouts (5s for serve startup), explicit `time.sleep(0.3-1.0s)` for fire-and-forget ingest completion (already established in `test_hook_fallback.py`), no shared state across MH classes.
- **macOS-only ground truth**: Phase 4 CI is Linux. If a test depends on macOS-specific behavior (e.g. `flock` absence), document it. None expected at this layer; subprocess and HTTP are portable.
- **MH-5 daemon teardown**: if a test fails between `serve --daemon` and `serve --stop`, a stray PID file + port can poison the next test. Use a finalizer fixture that always tries to stop the daemon, even on exception.

## Out of Scope (re-confirmed from kickoff and gap resolutions)

- Cross-platform CI matrix (macOS-developer-first per `docs/plan_v2.md`)
- Performance regression budget on `secondsight sync` for large fallback files
- Phase 2 analysis layer
- **Fallback events replay-into-DB** (G1-α confirmed; tracked as P1-13 scar carry-forward, not under GUR-99)
- Re-testing component behaviors covered by existing unit-test files

## Success Criteria

- All 5 must-haves green on developer macbook in `pytest tests/integration/test_phase1_e2e.py -v`
- 30 consecutive runs ≥ 29 green (≤ 1 flake permitted) — measured locally before merge
- Deliberately breaking a seam (e.g. URL typo, segment_index increment removal) turns at least one MH red with a failure message that names the broken seam
- No production code changes
- New test file ≤ 600 lines (honest target; alarm if exceeded — likely indicates over-testing)
