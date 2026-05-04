# Task 1: FastAPI Server Scaffold + Registry + Daemon Control (P1-5)

## Context

Read: overview.md (esp. "Per-project resource registry is lazy" and "Daemon = double-fork" decisions)

This task ships the *empty FastAPI app* — no domain endpoints yet, only `GET /health`, lifespan wiring, the per-project resource cache, and the `secondsight serve [--daemon|--stop|status]` Typer command. Subsequent tasks (P1-6, P1-7) attach the hook router and tracker dependency. We split this from P1-6 because daemon control + lifespan have failure modes that are unrelated to hook endpoints — testing them together makes the death tests muddy.

**Plan ref:** P1-5
**SD refs:** §8.3 (server deployment), §3.9 (pipeline orientation)

## Files

- Create: `src/secondsight/api/__init__.py`
- Create: `src/secondsight/api/server.py` — `create_app()` factory, lifespan ctx
- Create: `src/secondsight/api/registry.py` — `ProjectRegistry` (lazy DBEngine + repo + pipeline construction, per-project asyncio.Lock)
- Create: `src/secondsight/cli/__init__.py`
- Create: `src/secondsight/cli/serve.py` — Typer command group: `serve`, `serve --daemon`, `serve --stop`, `serve status` (also exposed as a top-level CLI in P1-12)
- Create: `src/secondsight/daemon.py` — `daemonize()` (double-fork), `read_pidfile()`, `write_pidfile_atomic()`, `stop_daemon(pid_path)` (SIGTERM with 5s grace, then SIGKILL)
- Create: `tests/api/__init__.py`
- Create: `tests/api/conftest.py` — `tmp_secondsight_home` fixture, ASGI test client
- Create: `tests/api/test_server_lifespan.py`
- Create: `tests/api/test_registry.py`
- Create: `tests/cli/__init__.py`
- Create: `tests/cli/test_serve_daemon.py`

## Public Contract

```python
# api/server.py
def create_app(
    *,
    secondsight_home: Path,
    config: ServerConfig | None = None,
    registry: ProjectRegistry | None = None,  # injectable for tests
) -> FastAPI:
    """Build a FastAPI app bound to the given SecondSight home directory."""

# api/registry.py
class ProjectRegistry:
    def __init__(self, secondsight_home: Path) -> None: ...

    async def get(self, project_id: str) -> ProjectResources:
        """Return cached resources for project_id, materializing on first use.
        Concurrent calls for the same NEW project_id share one DBEngine.
        """

    async def aclose(self) -> None:
        """Close all engines. Used by lifespan shutdown."""

@dataclass(frozen=True)
class ProjectResources:
    project_id: str
    db_engine: DBEngine
    events_repository: EventsRepository
    raw_trace_store: RawTraceStore
    sync_log: SyncLog
    pipeline: ObservationPipeline

# daemon.py
def daemonize(*, pid_path: Path, log_path: Path, on_child: Callable[[], None]) -> None:
    """Double-fork into background. Parent returns; child runs `on_child()`.
    Writes pid_path atomically (tmp+rename). Redirects stdout/stderr to log_path.
    """

@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    pid: int | None
    cmdline_match: bool   # PID exists AND its cmdline matches our executable
    uptime_seconds: int | None

def daemon_status(pid_path: Path) -> DaemonStatus: ...
def stop_daemon(pid_path: Path, *, grace_seconds: float = 5.0) -> bool: ...
```

## Death Test Requirements (write and verify red BEFORE production code)

1. **Stale-PID kill bug.** Write a PID file containing a real but unrelated PID (e.g. `os.getpid() + 1` after asserting it's an unrelated process — or use a fixture that fakes a `/proc`-style cmdline). `stop_daemon` MUST refuse to kill it (verify `cmdline_match=False` path).
2. **Concurrent-init race for the same new project_id.** 50 `asyncio.gather` calls to `registry.get("proj_X")` for a fresh `proj_X`. Assert: exactly **one** `DBEngine` is constructed (count via mock or by verifying only one `intelligence.db` file's mtime predates the others by ≥ 0).
3. **Lifespan shutdown leaks engines.** Build app, await startup, force `aclose()`; assert all per-project engines have had their `dispose()` called. Without this, file handles leak across test restarts.
4. **Double-fork inheritance leak.** Daemon child must close inherited file descriptors (stdin/stdout/stderr swapped to log file; no parent test-runner pipes leak in). Test by spawning the daemon under a parent that opens an extra fd and asserting the child cannot read from it. (POSIX-only; skip on Windows.)
5. **PID file written non-atomically corrupts on crash.** Test: simulate process death between `open()` and `write()` of the PID file (mock `Path.write_text` to raise mid-write). Assert: no half-written PID file remains; `daemon_status` returns `running=False`, not "garbage PID".
6. **`/health` lies about readiness during startup.** Issue a GET `/health` *before* lifespan startup completes (use FastAPI's `@app.on_event("startup")` lag, or inject a slow startup hook). Assert: either request blocks until startup finishes, or returns 503 — never 200 with stale state.

## Unit Test Requirements

- `create_app()` returns an app whose `GET /health` returns `{status, version, uptime_s}` after startup.
- `ProjectRegistry.get` is idempotent: 1000 sequential calls for the same project hit `DBEngine.__init__` exactly once.
- `ProjectRegistry.aclose` is idempotent: two consecutive calls do not raise.
- `daemonize` writes a PID file at the expected path with the child's PID.
- `stop_daemon` returns `True` when the daemon dies within grace; `False` and SIGKILLs after grace.
- `daemon_status` for a never-started daemon returns `running=False, pid=None`.

## Implementation Steps

- [ ] Step 1: STEP 0 — answer the four prerequisite questions in scar report draft
- [ ] Step 2: Write death tests (6 cases above)
- [ ] Step 3: Run death tests — verify red
- [ ] Step 4: Write unit tests
- [ ] Step 5: Run unit tests — verify red
- [ ] Step 6: Implement `daemon.py` (smallest viable double-fork; use `os.fork`, `os.setsid`, `os.dup2`)
- [ ] Step 7: Implement `ProjectRegistry` with per-project `asyncio.Lock` keyed in a registry-wide dict; the dict itself is guarded by a single lock for the brief append window
- [ ] Step 8: Implement `create_app()` and lifespan; wire registry into `app.state`
- [ ] Step 9: Implement Typer `serve` command (foreground + daemon + stop + status)
- [ ] Step 10: Run all tests — green
- [ ] Step 11: Write scar report
- [ ] Step 12: Self-iteration (Level 1) — fix task-scope items
- [ ] Step 13: Re-run tests — no regression

## Expected Scar Report Items

- Potential silent failure: `daemonize` on macOS may behave differently than Linux re: `os.fork()` + asyncio (we run `daemonize` BEFORE starting uvicorn, but verify via macOS-targeted CI smoke).
- Potential silent failure: PID file race when two `serve --daemon` calls happen concurrently — second one silently overwrites the first PID.
- Assumption to verify: `~/.secondsight/logs/` auto-creation. Who owns it?
- Potential shortcut: `ProjectRegistry.get` returns a *frozen* `ProjectResources`; we are NOT yet handling project-id eviction (memory grows linearly). Acceptable for Phase 1; defer eviction to Phase 2 with explicit policy.
- Boundary issue: `secondsight_home` must be absolute and writable; we should validate at startup, not at first-event time.

## Acceptance Criteria

- All death tests pass
- All unit tests pass
- `mypy` clean (project pre-commit)
- Scar report contains at least the items above with explicit `resolved_items` or `deferred_to_feature_iteration` flags
- Public contract docstrings match the implementation
- No imports from `secondsight.poc.*`
