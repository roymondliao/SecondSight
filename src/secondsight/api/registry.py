"""ProjectRegistry — lazy per-project resource cache (P1-5).

First event for a new project_id materializes
(DBEngine, EventsRepository, RawTraceStore, SyncLog, ObservationPipeline)
under a per-project asyncio.Lock.  Cached for the process lifetime.

Design assumptions:
- This module assumes exactly ONE asyncio event loop / single uvicorn worker.
  asyncio.Lock is NOT process-safe.  Multi-worker deployments are out of
  scope for Phase 1 (localhost-only, single-process daemon).
- secondsight_home must be absolute and writable.  This is validated at
  ProjectRegistry.__init__() time, not at first-event time, so the error
  surfaces during server startup rather than silently on the first request.
- Memory grows linearly with unique project_ids.  Eviction is deferred to
  Phase 2 (documented in scar report).

If these assumptions stop holding, the first thing to rot is:
  concurrent DBEngine construction for the same project (WAL race).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from secondsight.observation.pipeline import ObservationPipeline
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.sync_log import SyncLog


@dataclass(frozen=True)
class ProjectResources:
    """All per-project storage resources, materialized lazily."""

    project_id: str
    db_engine: DBEngine
    events_repository: EventsRepository
    raw_trace_store: RawTraceStore
    sync_log: SyncLog
    pipeline: ObservationPipeline


class ProjectRegistry:
    """Lazy, cached per-project resource registry.

    Thread-safety contract: asyncio.Lock per project for the
    initialization path.  The registry dict itself is guarded by a single
    asyncio.Lock during the brief "does this key exist?" + "insert" window.
    After a project is materialized, subsequent get() calls return the
    cached value without any lock contention.
    """

    def __init__(self, secondsight_home: Path) -> None:
        self._home = Path(secondsight_home)
        # Validate at construction time, not at first use.
        if not self._home.is_absolute():
            raise ValueError(
                f"secondsight_home must be absolute, got: {self._home!r}"
            )

        # Ensure the home directory exists.  Fail fast here so the server
        # doesn't start and then blow up on the first request.
        self._home.mkdir(parents=True, exist_ok=True)

        # Registry dict: project_id → ProjectResources
        self._registry: dict[str, ProjectResources] = {}

        # Per-project initialization locks.  Keyed by project_id.
        # A project_id that has never been seen gets a new lock on demand.
        self._project_locks: dict[str, asyncio.Lock] = {}

        # Single lock guarding _project_locks dict access only (brief window).
        self._locks_guard: asyncio.Lock = asyncio.Lock()

        # Closed flag — set by aclose(); prevents new materializations
        # after shutdown.
        self._closed: bool = False

    # NOTE: this two-level locking pattern is duplicated at:
    #   - observation/tracker.py:156 (SessionTracker._get_or_create_state)
    #   - api/server.py (AppState.get_or_create_tracker)
    # Phase 2 / iteration will consolidate into a shared _LazyCacheWithLocking utility.
    # Until then: changes to the locking logic MUST be applied to ALL THREE sites.
    async def get(self, project_id: str) -> ProjectResources:
        """Return cached resources for project_id, materializing on first use.

        Concurrent calls for the same NEW project_id share one DBEngine.
        A per-project Lock ensures only one DBEngine is constructed even
        under a thundering herd of concurrent first-requests.

        Raises:
            RuntimeError: if the registry has been closed via aclose().
        """
        if self._closed:
            raise RuntimeError(
                "ProjectRegistry has been closed; cannot serve requests."
            )

        # Fast path is safe ONLY when both assumptions hold:
        #   (1) Single asyncio event loop — no OS-thread concurrency on _registry.
        #       (See module docstring; multi-worker uvicorn breaks this.)
        #   (2) CPython's GIL makes dict.__contains__ atomic for hashable keys.
        # A future multi-worker migration MUST update both assumptions, not just (1).
        if project_id in self._registry:
            return self._registry[project_id]

        # Slow path: first request for this project_id.
        # 1. Ensure a per-project lock exists.
        async with self._locks_guard:
            if project_id not in self._project_locks:
                self._project_locks[project_id] = asyncio.Lock()
        project_lock = self._project_locks[project_id]

        # 2. Acquire the per-project lock so only one coroutine materializes.
        async with project_lock:
            # Double-check inside the lock — another coroutine may have
            # materialized while we were waiting.
            if project_id in self._registry:
                return self._registry[project_id]

            logger.info(
                "Materializing resources for new project: {pid}", pid=project_id
            )
            resources = await asyncio.to_thread(
                self._build_resources, project_id
            )
            self._registry[project_id] = resources
            return resources

    def _build_resources(self, project_id: str) -> ProjectResources:
        """Synchronous resource construction (runs in a thread pool).

        Assumption: project_id is a safe path component.  The adapter
        layer (Phase 1.3) is responsible for validating project_ids before
        they reach the registry.  We do a minimal sanity check here.
        """
        project_dir = self._home / "projects" / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        db_path = project_dir / "intelligence.db"
        db_engine = DBEngine(db_path=db_path)

        repo = EventsRepository(db_engine=db_engine)
        repo.create_schema()  # idempotent

        rts = RawTraceStore(project_root=project_dir)

        sync_log_path = project_dir / "sync.log"
        sync_log = SyncLog(path=sync_log_path)

        pipeline = ObservationPipeline(
            raw_trace_store=rts,
            events_repository=repo,
            sync_log=sync_log,
        )

        return ProjectResources(
            project_id=project_id,
            db_engine=db_engine,
            events_repository=repo,
            raw_trace_store=rts,
            sync_log=sync_log,
            pipeline=pipeline,
        )

    async def aclose(self) -> None:
        """Close all materialized engines.  Idempotent.

        Called by the lifespan shutdown hook.  After aclose(), get()
        raises RuntimeError.
        """
        if self._closed:
            # Already closed — idempotent.
            return

        self._closed = True
        for project_id, resources in list(self._registry.items()):
            try:
                resources.db_engine.dispose()
                logger.debug("Disposed DBEngine for project: {pid}", pid=project_id)
            except Exception as exc:
                # Best-effort — log but do not raise so all engines get a
                # chance to close.
                logger.warning(
                    "Error disposing DBEngine for project {pid}: {err}",
                    pid=project_id,
                    err=exc,
                )


__all__ = ["ProjectRegistry", "ProjectResources"]
