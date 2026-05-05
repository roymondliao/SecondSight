# Overview: GUR-99 Phase 1 End-to-End Integration Test

## Goal

Add a seam-level test layer at `tests/integration/test_phase1_e2e.py` that proves the user-visible install-and-run lifecycle composes correctly across hook-script, API-server, storage, and CLI modules.

## Architecture

One new test file imports the existing `real_secondsight_server` fixture from `tests/scripts/conftest.py`. Each must-have (MH-1..MH-5) maps to one test class. No production code changes — if a test cannot go green without one, that change splits into a prerequisite task.

## Tech Stack

- pytest + uvicorn + FastAPI
- subprocess + sockets for live e2e (no in-process emulation of bash)
- typer.testing.CliRunner for `secondsight init`/`sync`/`status`
- subprocess.Popen for `serve --daemon` (daemon mode forks)

## Key Decisions

- **Test bash, not Python**: every "hook fires" assertion invokes `subprocess.run(['bash', script])`. `client.post()` against FastAPI TestClient is forbidden — bypasses the seams under test.
- **G1-α (board-confirmed)**: MH-3 tests archive-only fallback recovery. Replay-into-DB is Phase 1 carry-forward, not under GUR-99.
- **G2 correction**: MH-2 sequence interleaves `user-prompt` events because `segment_index` only increments on `USER_PROMPT`.
- **G3 correction**: MH-2 uses explicit `sub_agent_start`/`sub_agent_end` events with `data["sub_agent_id"]`, plus a stack-mismatch death case.
- **G4-β (board-confirmed)**: MH-4 prints latency histogram to stderr. No on-disk artifact.
- **G5-β (board-confirmed)**: New `tests/integration/` package. Fixtures imported from `tests.scripts.conftest`.
- **Latency budget = p95 ≤ 50ms** (CI-stable proxy for SD §3.9.1's 7ms theoretical). MH-4 docstring documents removal-when-prod-metric-exists.

## Death Cases Summary

1. **Hook URL drift** — `pre-tool-use.sh` posts to `/hook/pre_tool_use` while server route is `/hook/pre-tool-use` → all events silently fall back to JSONL. MH-1 catches.
2. **`segment_index` frozen at 0** — `tracker.bind()` returns cached value without `USER_PROMPT` increment → all rows share segment_index=0. MH-2 catches.
3. **Sync archives fallback before commit** — `archive_fallback_events()` moves the file before `.bak` is durable → mid-archive crash loses pending work. MH-3 catches.

## File Map

- `tests/integration/__init__.py` — new (empty package marker)
- `tests/integration/test_phase1_e2e.py` — new (5 test classes: `TestMH1SingleEvent`, `TestMH2MultiEvent`, `TestMH3FallbackArchive`, `TestMH4LatencyBudget`, `TestMH5CliLifecycle`)
- `tests/integration/_prereqs.py` — new (PATH-tool detection helper, named-skip)
- `pyproject.toml` — modify if `[tool.pytest.ini_options].testpaths` does not already include `tests/`

## Out of Scope

- Cross-platform CI matrix (macOS-developer-first per `docs/plan_v2.md`)
- Phase 2 analysis layer
- Fallback events replay-into-DB (G1-α; tracked as P1-13 scar carry-forward)
- Re-testing component behaviors covered by `tests/installer/`, `tests/cli/test_serve_daemon.py`, `tests/observation/test_tracker.py`, `tests/storage/`, `tests/scripts/test_hook_fallback.py` (DT-1..9, UT-2..8)
