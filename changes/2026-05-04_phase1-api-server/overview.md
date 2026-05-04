# Overview: Phase 1 — API Server Core (GUR-96)

## Goal

Stand up the HTTP server that turns coding-agent hook events into durable observations. Phase 1 storage already exposes `ObservationPipeline.ingest(Event)`; this change builds the *thin slice* that gets agent payloads from a bash hook over `localhost:8420` into that pipeline — and ensures a missing server never costs the user data.

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Hook script (~/.claude/hooks/pre-tool-use.sh)                             │
│  curl --connect-timeout 0.1 → 127.0.0.1:8420 → on failure: append JSONL    │
└─────────────────────────────────────────────────┬──────────────────────────┘
                                                  │ HTTP POST /hook/{type}
                                                  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  FastAPI app (api/server.py)                                               │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  POST /hook/{event_type}  (api/hooks.py)                             │  │
│  │    1. validate envelope (project_id required)                        │  │
│  │    2. normalizer.normalize(event_type, payload) → Event              │  │
│  │    3. tracker.bind(event)  ← assigns segment_index / depth / sub-id  │  │
│  │    4. asyncio.create_task(pipeline.ingest(event))                    │  │
│  │    5. return {"status": "ok"}   ← hook unblocks immediately          │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  GET /health  → {"status": "ok", "version": ..., "uptime_s": ...}    │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                            │
│  Lifespan:                                                                 │
│    startup  → load global config, build per-project registry (lazy)        │
│    shutdown → drain in-flight ingest tasks (bounded wait), close engines   │
└────────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
                                        ObservationPipeline (P1-4)
                                                 + EventsRepository
                                                 + RawTraceStore
                                                 + SyncLog
```

## Tech Stack

- FastAPI + uvicorn (already in `pyproject.toml`).
- Pydantic v2 for request envelopes.
- Typer entrypoint at `src/secondsight/cli/serve.py` for daemon control (full CLI in P1-12).
- `loguru` for structured logging; daemon stdout/stderr redirected to `~/.secondsight/logs/server.log`.
- No new runtime deps. (Test deps `httpx`, `pytest-asyncio` already pulled in by FastAPI[standard] / pyproject.)

## Key Decisions

- **The hook's only durability promise is "either the server got it or the JSONL file got it."** The server-side promise (FS-first) is unchanged from Phase 1.1. We deliberately do not mirror the FS-first contract on the client side — the bash hook is too primitive to make atomic guarantees, so it makes a *coarser* one (write-or-fallback) and lets the server own the rigorous side.
- **Normalizer is a Protocol, not a class hierarchy.** P1-9..P1-11 will land real adapters; for this change we ship `IdentityNormalizer` that requires a fully-formed Event envelope (used by tests + the `claude-code` adapter once it lands). The seam is:
  ```python
  class Normalizer(Protocol):
      def supports(self, event_type: str) -> bool: ...
      def normalize(self, event_type: str, payload: dict) -> Event: ...
  ```
  The server holds a `NormalizerRegistry` keyed by agent type (header `X-SecondSight-Agent: claude-code`).
- **SessionTracker is process-local, but warm-starts from DB.** First event for a `(project_id, session_id)` pair after restart triggers `events_repo.get_max_segment_index(session_id)` and resumes counting. Cost: one cheap indexed read per cold session. Benefit: tracker state is never authoritative — DB is.
- **Hook response returns *before* ingest completes.** This is the latency contract. We use `asyncio.create_task(pipeline.ingest(event))` and structurally guarantee the route handler does not await it. A weakref set tracks in-flight tasks so the lifespan shutdown can drain them.
- **Daemon = double-fork.** PID file at `~/.secondsight/server.pid`. `--stop` SIGTERMs the PID, waits up to 5s, falls back to SIGKILL on timeout. `status` reads PID, checks `/proc`-equivalent (`os.kill(pid, 0)`), and probes `/health` over HTTP.
- **Per-project resource registry is lazy.** First event for a new `project_id` materializes `(DBEngine, EventsRepository, RawTraceStore, SyncLog, ObservationPipeline)` under a per-project asyncio lock. Cached for the process's life.
- **Bind is localhost-only.** No auth layer, no CORS in Phase 1. Documented.

## Death Cases Summary

These are the silent-failure paths this change must instrument with explicit tests:

1. **Hook latency contract violated by accidental `await`.** A future contributor swaps `asyncio.create_task(pipeline.ingest(...))` for `await pipeline.ingest(...)`. Detection: a death test with a `pipeline.ingest` that blocks on an `asyncio.Event`; the route handler must respond within a tight bound (e.g. 50ms) while ingest is still running.
2. **Fallback file accumulates events that no later step ever reads.** The JSONL grows forever, and nobody notices until disk fills. Detection: a hook-script death test asserts the fallback file contains a structured envelope with `agent`, `event_type`, `timestamp`, `payload` — i.e. *enough* for backfill (P1-13). Pure-payload appends are rejected.
3. **Server crash mid-request leaves orphan ingest task.** `asyncio.create_task` without `add_done_callback` swallows exceptions; FS write fails, nobody knows. Detection: death test injects an FS-write `OSError`, verifies a structured error log is emitted via the task's done-callback. (`pipeline.ingest`'s own contract re-raises; we just need to make sure the task doesn't get GC'd silently.)
4. **Tracker desync after server restart.** Server restarts mid-session; first event after restart gets `segment_index=0`, overwriting the row index. Detection: insert N events, kill server, restart, send next event, verify `segment_index >= max(prior)`.
5. **Two `project_id`s racing to first-init the registry.** Two requests for new projects arrive concurrently → two `DBEngine`s for the same project → SQLite WAL race. Detection: death test fires 50 concurrent requests for two new projects, asserts exactly two `DBEngine.__init__` calls.
6. **Daemon `--stop` orphans the child.** PID file points at a stale PID (process already gone, PID reused). `--stop` kills an unrelated process. Detection: death test fakes a PID file with current PID + 1, asserts `--stop` checks process identity (e.g. via stored start-timestamp / cmdline) before killing.
7. **Path traversal in `event_type`.** `POST /hook/../../etc/passwd` is rejected at the routing layer (FastAPI handles this) but a literal `event_type` carrying `..` could still hit `EventType()` and explode. Detection: enumerate the typed enum + 400 on unknown event_type, never raw-string it into a path.
8. **Hook script exits non-zero on fallback.** If the bash script `exit 1`s when the server is down, the agent's tool call fails — observation became *destructive*. Detection: shellcheck + integration test running the hook with no server, asserts exit 0.

## File Map

### Source — production
- `src/secondsight/api/__init__.py`
- `src/secondsight/api/server.py` — FastAPI app factory, lifespan, registry wiring
- `src/secondsight/api/hooks.py` — `POST /hook/{event_type}` router
- `src/secondsight/api/registry.py` — per-project resource cache (DBEngine + repo + pipeline)
- `src/secondsight/api/normalizer.py` — `Normalizer` Protocol + `IdentityNormalizer` + registry
- `src/secondsight/api/schemas.py` — request envelopes (Pydantic)
- `src/secondsight/observation/tracker.py` — `SessionTracker` (in-memory, DB-warm-started)
- `src/secondsight/cli/__init__.py`
- `src/secondsight/cli/serve.py` — `secondsight serve` Typer command (daemon/--stop/status)
- `src/secondsight/daemon.py` — double-fork helpers, PID file, signal logic
- `scripts/hooks/pre-tool-use.sh` — example fallback-aware hook (also the integration-test artifact)
- `scripts/hooks/_lib.sh` — shared bash helpers (curl call + JSONL append)

### Tests
- `tests/api/test_server_lifespan.py`
- `tests/api/test_hooks_endpoint.py`
- `tests/api/test_registry.py`
- `tests/api/test_latency_contract.py` ← death tests for #1, #3
- `tests/observation/test_tracker.py`
- `tests/cli/test_serve_daemon.py` ← death tests for #6
- `tests/scripts/test_hook_fallback.py` ← bash via subprocess; covers #2, #8
- `tests/api/conftest.py` — fixtures: ASGI test client, project tmpdir, fake normalizer

### Design notes (this change)
- `changes/2026-05-04_phase1-api-server/api-design.md` — written during implementation if non-trivial decisions arise

## Non-Goals (explicit)

- Real Claude Code adapter (P1-9..P1-11 — separate change).
- `secondsight init` (P1-11) and `secondsight sync` (P1-13).
- Auth, TLS, multi-host bind.
- Connection pooling beyond the single-connection-per-DBEngine pattern from Phase 1.1.
- Hot-reload / config watch.
- Cross-platform daemonization (Windows). POSIX only this phase.
- Microbenchmark of hook latency in CI; we verify the structural property only.

## Carried-Forward Assumptions (from Pre-thinking gate)

- **G1**: Daemon log path is `~/.secondsight/logs/server.log`; rotation deferred.
- **G2**: Per-project `intelligence.db` is lazy-created on first event for that project.
- **G3**: Localhost-only bind is the auth surface; no token/origin checks in Phase 1.
- **U1**: Per-project locks for the registry (not a global lock).
