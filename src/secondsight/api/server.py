"""FastAPI application factory for the SecondSight server (P1-5).

Design assumptions:
- create_app() is called ONCE per process.  If called multiple times,
  each call returns a fresh app with its own lifespan/registry.
- secondsight_home must be absolute.  Validated at create_app() time.
- The registry is injectable for tests (pass a pre-built ProjectRegistry).
  In production, pass None and the factory creates one.
- AppState is a mutable dataclass (not frozen) because inflight_tasks is a
  set that is mutated by route handlers throughout the server's lifetime.
  All other fields are written once at startup and read-only thereafter.

Silent failure conditions:
- If lifespan startup raises, uvicorn will shut the app down but the
  caller may not see a clear error in the log unless loguru is configured.
  This is acceptable for Phase 1; structured startup logging is deferred.
- The bounded drain timeout (1s) at shutdown is arbitrary. A burst of slow
  ingests right before shutdown will be cancelled. Acceptable for Phase 1:
  raw trace is already on disk (FS-first contract in ObservationPipeline).
- The per-project SessionTracker cache uses
  :class:`secondsight._common.lazy_cache.LazyCacheWithLocking` (the same
  utility now backing ProjectRegistry and SessionTracker). Locking
  assumptions live there, not duplicated here.
- The Sweeper (D10) iterates only projects that have been materialized by
  the registry (i.e., have received at least one event since server startup).
  Sessions whose project has never served a request are not swept. This is
  an acceptable v1 limitation: in practice the server is started after the
  first event ingest, so all active projects are already materialized.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from secondsight._common.lazy_cache import LazyCacheWithLocking
from secondsight.adapters import (
    AdapterRegistry,
    ClaudeCodeAdapter,
    CodexAdapter,
    IdentityAdapter,
    OpenCodeAdapter,
)
from secondsight.api.registry import ProjectRegistry
from secondsight.observation.tracker import SessionTracker

_VERSION = "0.1.0"

# Bounded drain timeout at shutdown (seconds).
# In-flight ingest tasks that do not complete within this window are cancelled.
# The raw trace is already on disk (FS-first) so cancellation is safe.
_SHUTDOWN_DRAIN_TIMEOUT_S = 1.0


class ServerConfig:
    """Runtime configuration for the server.  All fields have defaults."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8420,
        dashboard_dist: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.dashboard_dist = dashboard_dist


class AppState:
    """Typed contract for app.state assigned during lifespan startup.

    All scalar fields are written exactly once (at startup).
    inflight_tasks is a strong-reference set mutated by route handlers.
    Each task adds itself on creation and removes itself via a done_callback
    (discard), so the set never accumulates completed tasks.
    The per-project tracker cache is mutated lazily by get_or_create_tracker().

    Route handlers access via ``request.app.state.server_state.<field>``.

    NOTE: This class is not frozen (unlike the original dataclass) because
    inflight_tasks (set) and the tracker cache must be mutated at runtime.
    The startup_time, registry, secondsight_home, config, and
    adapter_registry fields are write-once and should not be reassigned
    after startup.
    """

    def __init__(
        self,
        *,
        registry: ProjectRegistry,
        startup_time: float,
        secondsight_home: Path,
        config: ServerConfig,
        adapter_registry: AdapterRegistry,
    ) -> None:
        self.registry = registry
        self.startup_time = startup_time
        self.secondsight_home = secondsight_home
        self.config = config
        self.adapter_registry = adapter_registry
        # Strong-reference set: prevents GC from collecting tasks before shutdown
        # drain can enumerate them. Each task registers a discard done_callback
        # so completed tasks are removed and the set does not grow unboundedly.
        # Using WeakSet here would let completed tasks be GC'd and disappear from
        # the set before the shutdown drain snapshot — the drain would then report
        # 0 pending tasks even if tasks had been abandoned silently.
        self.inflight_tasks: set[asyncio.Task[None]] = set()

        # Sweeper background task (D10: Sweeper lifecycle). Set during lifespan
        # startup; None before startup or after shutdown. Typed explicitly so
        # routes and tests can assert the sweeper was started (not a silent None).
        self.sweeper_task: asyncio.Task[None] | None = None

        # Per-project SessionTracker cache. Same two-level locking pattern
        # as ProjectRegistry, materialised through the shared utility — see
        # :mod:`secondsight._common.lazy_cache` for the locking contract.
        self._tracker_cache: LazyCacheWithLocking[str, SessionTracker] = LazyCacheWithLocking(
            materialiser=self._materialise_tracker
        )

    async def _materialise_tracker(self, project_id: str) -> SessionTracker:
        """Build a SessionTracker bound to ``project_id``'s events_repository.

        Resources are resolved through ``self.registry`` so the materialiser
        depends only on ``project_id`` (matching the cache contract). The
        registry's own cache makes the inner ``get`` a fast-path dict
        lookup once the project has been seen.
        """
        resources = await self.registry.get(project_id)
        repo = resources.events_repository

        async def warm_start(session_id: str) -> int | None:
            return await asyncio.to_thread(repo.get_max_segment_index, session_id)

        return SessionTracker(warm_start=warm_start)

    async def get_or_create_tracker(self, project_id: str) -> SessionTracker:
        """Return cached SessionTracker for project_id, creating on first use.

        The WarmStart closure inside the materialiser closes over the
        project's events_repository so each project's tracker warm-starts
        from that project's DB.
        """
        return await self._tracker_cache.get(project_id)


# ---------------------------------------------------------------------------
# Sweeper coordinator — timeout-based fallback for crashed-agent sessions (D10)
# ---------------------------------------------------------------------------

# Sweeper defaults. Can be overridden via environment variables in a future
# config extension; hardcoded here for v1 (single-process, localhost-only).
_SWEEPER_INTERVAL_SECONDS: int = 60
_SWEEPER_SESSION_TIMEOUT_MINUTES: int = 30
_SWEEPER_SHUTDOWN_TIMEOUT_S: float = 5.0


def _resolve_dashboard_dist(configured_path: Path | None) -> Path | None:
    """Return the built dashboard directory if available.

    Resolution order:
    1. Explicit ``ServerConfig.dashboard_dist`` for tests/dev overrides.
    2. Packaged assets at ``secondsight/_resources/dashboard``.
    3. Repo-local Vite output at ``frontend/dist``.

    Returning ``None`` is intentional: the API server must still boot
    cleanly on environments where the frontend has not been built yet.
    """
    candidates: list[Path] = []
    if configured_path is not None:
        candidates.append(Path(configured_path))

    package_assets = Path(__file__).resolve().parents[1] / "_resources" / "dashboard"
    repo_assets = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    candidates.extend([package_assets, repo_assets])

    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


class _ServerSweepCoordinator:
    """Cross-project Sweeper coordinator (D10).

    Runs as a background asyncio.Task in the server lifespan. Each tick:
    1. Get the list of materialized project_ids from the registry.
    2. For each project, use the project's EventsRepository to find stale sessions.
    3. Filter out sessions with a terminal analysis_runs row.
    4. Dispatch timeout recovery through the project's shared Trigger.

    Silent failure conditions:
    - Projects that have not yet received any event are not in the registry
      and are not scanned. This is the correct behavior: if the project has
      no events, there are no sessions to sweep.
    - If the registry has been closed (server shutting down), the scan is
      skipped gracefully.
    """

    def __init__(
        self,
        registry: "ProjectRegistry",
        secondsight_home: Path,
        interval_seconds: int = _SWEEPER_INTERVAL_SECONDS,
        session_timeout_minutes: int = _SWEEPER_SESSION_TIMEOUT_MINUTES,
    ) -> None:
        self._registry = registry
        self._secondsight_home = secondsight_home
        self._interval_seconds = interval_seconds
        self._session_timeout_minutes = session_timeout_minutes

    async def run(self) -> None:
        """Main sweep loop. Runs until cancelled (asyncio.CancelledError propagates)."""
        from datetime import datetime, timedelta, timezone

        logger.info(
            "Sweeper coordinator running: interval={interval}s, timeout={timeout}min",
            interval=self._interval_seconds,
            timeout=self._session_timeout_minutes,
        )
        while True:
            try:
                now = datetime.now(tz=timezone.utc)
                cutoff = now - timedelta(minutes=self._session_timeout_minutes)
                await self._sweep_all_projects(cutoff)
            except asyncio.CancelledError:
                logger.info("Sweeper coordinator cancelled during sweep.")
                raise
            except Exception as exc:
                logger.error(
                    "Sweeper coordinator sweep error: {type}: {exc}",
                    type=type(exc).__name__,
                    exc=exc,
                )

            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                logger.info("Sweeper coordinator cancelled during sleep.")
                raise

    async def _sweep_all_projects(self, cutoff: datetime) -> None:
        """One sweep tick across all materialized projects."""
        from secondsight.analysis.schemas import TERMINAL_STAGES

        project_ids = self._registry.materialized_project_ids()
        if not project_ids:
            logger.debug("Sweeper: no materialized projects to sweep.")
            return

        for project_id in project_ids:
            try:
                resources = await self._registry.get(project_id)
                candidates = resources.events_repository.find_stale_session_candidates(
                    project_id=project_id,
                    last_event_before=cutoff,
                )
                if resources.analysis_runtime is None:
                    if candidates:
                        logger.warning(
                            "Sweeper: project_id={pid} has {count} stale session(s) "
                            "but analysis runtime is unavailable: {err}",
                            pid=project_id,
                            count=len(candidates),
                            err=resources.analysis_runtime_error or "unknown error",
                        )
                    continue

                runs_repo = resources.analysis_runtime.analysis_runs_repository
                trigger = resources.analysis_runtime.trigger

                for pid, session_id, last_event_ts in candidates:
                    try:
                        latest_run = runs_repo.get_latest_for_session(session_id)
                        if latest_run is not None and latest_run.stage.value in TERMINAL_STAGES:
                            logger.debug(
                                "Sweeper: stale candidate already terminal — "
                                "project_id={pid} session_id={sid} stage={stage}",
                                pid=pid,
                                sid=session_id,
                                stage=latest_run.stage.value,
                            )
                            continue

                        result = await trigger.dispatch(
                            pid,
                            session_id,
                            source="timeout",
                        )
                        if result.dispatched:
                            logger.info(
                                "Sweeper: timeout recovery dispatched — project_id={pid} "
                                "session_id={sid} last_event_ts={ts}",
                                pid=pid,
                                sid=session_id,
                                ts=last_event_ts,
                            )
                        else:
                            logger.info(
                                "Sweeper: timeout recovery skipped — project_id={pid} "
                                "session_id={sid} last_event_ts={ts} reason={reason} "
                                "existing_stage={stage}",
                                pid=pid,
                                sid=session_id,
                                ts=last_event_ts,
                                reason=result.reason,
                                stage=result.existing_stage,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Sweeper: timeout recovery failed — project_id={pid} "
                            "session_id={sid} last_event_ts={ts} error={type}: {exc}",
                            pid=pid,
                            sid=session_id,
                            ts=last_event_ts,
                            type=type(exc).__name__,
                            exc=exc,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Sweeper: error sweeping project_id={pid}: {type}: {exc}",
                    pid=project_id,
                    type=type(exc).__name__,
                    exc=exc,
                )


def create_app(
    *,
    secondsight_home: Path,
    config: ServerConfig | None = None,
    registry: ProjectRegistry | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to the given SecondSight home directory.

    Args:
        secondsight_home: Absolute path to the SecondSight home dir
            (~/.secondsight in production).
        config: Optional ServerConfig; defaults to localhost:8420.
        registry: Injectable ProjectRegistry for tests.  If None, a new
            registry is created bound to secondsight_home.

    Returns:
        A fully configured FastAPI application.  Lifespan is wired; call
        startup before serving requests (TestClient does this automatically).
    """
    from secondsight.api.analysis import router as analysis_router
    from secondsight.api.directives import router as directives_router
    from secondsight.api.hooks import router as hooks_router
    from secondsight.api.injection import router as injection_router
    from secondsight.api.observation import router as observation_router

    home = Path(secondsight_home)
    if not home.is_absolute():
        raise ValueError(f"secondsight_home must be an absolute path, got: {home!r}")

    cfg = config or ServerConfig()
    reg = registry or ProjectRegistry(secondsight_home=home)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # --- Startup ---
        startup_time = time.monotonic()
        logger.info(
            "SecondSight server starting up (home={home}, host={host}, port={port})",
            home=home,
            host=cfg.host,
            port=cfg.port,
        )
        # Sweeper wiring (D10): start the cross-project sweep coordinator.
        # The coordinator iterates all projects that have been materialized
        # by the registry (i.e., have received at least one event).
        # See _ServerSweepCoordinator for the per-project dispatch logic.

        # Build adapter registry. Order is significant for first-match-wins
        # dispatch (plan §1 decision 5):
        #   1. ClaudeCodeAdapter — agent="claude_code" (P1-10, GUR-124).
        #   2. IdentityAdapter   — agent="test" baseline.
        # ClaudeCodeAdapter precedes IdentityAdapter so that, if a future
        # IdentityAdapter ever broadens its `supports()` to claim
        # agent="claude_code" (it does not today, but the universal-test
        # invariant could drift), the canonical adapter still wins dispatch.
        # The two publish overlapping event_types (Identity = full EventType
        # set; Claude = P1 floor subset) so registering Identity second emits
        # a benign RuntimeWarning (different agent gates → no real shadow);
        # we suppress it here with an explanatory comment so the warning
        # remains load-bearing for genuine same-agent shadows. Real future
        # adapters (Codex, OpenCode) follow the same pattern: register
        # before IdentityAdapter.
        import warnings

        adapter_registry = AdapterRegistry()
        adapter_registry.register(ClaudeCodeAdapter())
        with warnings.catch_warnings():
            # Benign overlap: IdentityAdapter publishes the full EventType set
            # for agent="test"; ClaudeCodeAdapter publishes the P1 floor for
            # agent="claude_code". Different agent gates prevent any dispatch
            # collision — see AdapterRegistry.register() docstring.
            warnings.simplefilter("ignore", RuntimeWarning)
            adapter_registry.register(CodexAdapter())
            adapter_registry.register(OpenCodeAdapter())
            adapter_registry.register(IdentityAdapter())

        # Assign the typed state contract once; inflight_tasks is initialized
        # as an empty strong-reference set inside AppState.__init__.
        server_state = AppState(
            registry=reg,
            startup_time=startup_time,
            secondsight_home=home,
            config=cfg,
            adapter_registry=adapter_registry,
        )
        app.state.server_state = server_state

        # Start the Sweeper coordinator (D10). This is the timeout-based fallback
        # path for sessions whose agent crashed before SESSION_END.
        # The coordinator uses registry.materialized_project_ids() each tick so
        # it automatically picks up new projects as they are materialized.
        sweep_coordinator = _ServerSweepCoordinator(
            registry=reg,
            secondsight_home=home,
        )
        server_state.sweeper_task = asyncio.create_task(
            sweep_coordinator.run(),
            name="sweeper-coordinator",
        )
        logger.info("Sweeper coordinator started.")

        logger.info("SecondSight server ready.")
        yield
        # --- Shutdown ---
        logger.info("SecondSight server shutting down...")

        # Drain in-flight ingest tasks with bounded timeout.
        # Snapshot the set now — completed tasks have already been discarded via
        # their done_callbacks, so only truly in-flight tasks remain here.
        pending = list(app.state.server_state.inflight_tasks)
        if pending:
            logger.info(
                "Draining {n} in-flight ingest task(s) (timeout={t}s)...",
                n=len(pending),
                t=_SHUTDOWN_DRAIN_TIMEOUT_S,
            )
            done, still_pending = await asyncio.wait(
                pending,
                timeout=_SHUTDOWN_DRAIN_TIMEOUT_S,
            )
            if still_pending:
                logger.warning(
                    "{n} ingest task(s) did not complete within {t}s; cancelling.",
                    n=len(still_pending),
                    t=_SHUTDOWN_DRAIN_TIMEOUT_S,
                )
                for task in still_pending:
                    task.cancel()
                # Brief wait for cancellation to propagate
                await asyncio.gather(*still_pending, return_exceptions=True)
                logger.info(
                    "Cancelled {n} ingest task(s).",
                    n=len(still_pending),
                )
            else:
                logger.info("All in-flight ingest tasks completed.")

        # Cancel the Sweeper coordinator with bounded timeout.
        sweeper_task = app.state.server_state.sweeper_task
        if sweeper_task is not None and not sweeper_task.done():
            sweeper_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(sweeper_task), timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                pass
            logger.info("Sweeper coordinator stopped.")

        await reg.aclose()
        logger.info("SecondSight server stopped.")

    app = FastAPI(
        title="SecondSight",
        version=_VERSION,
        lifespan=lifespan,
    )

    app.include_router(injection_router)
    # Mount the hooks router
    app.include_router(hooks_router)
    # Mount the observation router (GUR-147 task-A5)
    app.include_router(observation_router)
    # Mount the directives router (GUR-104 task-2)
    app.include_router(directives_router)
    # Mount the analysis router (GUR-104 task-3)
    app.include_router(analysis_router)

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------

    dashboard_dist = _resolve_dashboard_dist(cfg.dashboard_dist)
    if dashboard_dist is not None:
        app.mount(
            "/dashboard",
            StaticFiles(directory=str(dashboard_dist), html=True),
            name="dashboard",
        )

        @app.get("/")
        async def dashboard_root() -> RedirectResponse:
            return RedirectResponse(url="/dashboard/", status_code=307)

    @app.get("/health")
    async def health() -> dict:
        """Return server liveness status.

        This is a liveness probe: the server process is up and lifespan
        has completed.  It is NOT a readiness probe (per-project DBs are
        NOT checked here — deferred to Phase 2).

        Returns 200 with {liveness, version, uptime_s} after startup.
        FastAPI's lifespan contract guarantees this route is not reachable
        until the lifespan startup has completed, so we never return 200
        with uninitialized state.
        """
        uptime = time.monotonic() - app.state.server_state.startup_time
        return {
            "liveness": "alive",
            "version": _VERSION,
            "uptime_s": round(uptime, 3),
        }

    return app


__all__ = ["AppState", "ServerConfig", "create_app"]
