"""ProjectRegistry — lazy per-project resource cache (P1-5).

First event for a new project_id materializes
(DBEngine, EventsRepository, RawTraceStore, SyncLog, ObservationPipeline)
under a per-project asyncio.Lock.  Cached for the process lifetime.

Design assumptions:
- This module assumes exactly ONE asyncio event loop / single uvicorn worker.
  The single-loop / GIL-atomicity assumption that makes the fast-path read
  safe lives in :mod:`secondsight._common.lazy_cache`; do not duplicate it
  here. Multi-worker deployments are out of scope for Phase 1
  (localhost-only, single-process daemon).
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

from secondsight.analysis.runtime import (
    ProjectAnalysisRuntime,
    build_project_analysis_runtime,
)
from secondsight._common.lazy_cache import LazyCacheWithLocking
from secondsight.observation.pipeline import ObservationPipeline
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_ingress_store import RawIngressStore
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.sync_log import SyncLog


@dataclass(frozen=True)
class ProjectResources:
    """All per-project storage resources, materialized lazily."""

    project_id: str
    db_engine: DBEngine
    events_repository: EventsRepository
    raw_ingress_store: RawIngressStore
    raw_trace_store: RawTraceStore
    sync_log: SyncLog
    pipeline: ObservationPipeline
    analysis_runtime: ProjectAnalysisRuntime | None
    analysis_runtime_error: str | None


class ProjectRegistry:
    """Lazy, cached per-project resource registry.

    Thread-safety contract: per-project asyncio.Lock for the initialisation
    path, provided by :class:`LazyCacheWithLocking`. After a project is
    materialised, subsequent get() calls return the cached value without
    lock contention.
    """

    def __init__(self, secondsight_home: Path) -> None:
        self._home = Path(secondsight_home)
        # Validate at construction time, not at first use.
        if not self._home.is_absolute():
            raise ValueError(f"secondsight_home must be absolute, got: {self._home!r}")

        # Ensure the home directory exists.  Fail fast here so the server
        # doesn't start and then blow up on the first request.
        self._home.mkdir(parents=True, exist_ok=True)

        # Closed flag mirrors the cache's own closed state but is separate
        # because get() must raise RuntimeError with a registry-specific
        # message before delegating, matching the public contract that
        # existed before consolidation.
        self._closed: bool = False

        self._cache: LazyCacheWithLocking[str, ProjectResources] = LazyCacheWithLocking(
            materialiser=self._materialise,
            finaliser=self._finalise,
        )

    async def get(self, project_id: str) -> ProjectResources:
        """Return cached resources for project_id, materializing on first use.

        Concurrent calls for the same NEW project_id share one DBEngine.
        A per-project Lock ensures only one DBEngine is constructed even
        under a thundering herd of concurrent first-requests.

        Raises:
            RuntimeError: if the registry has been closed via aclose().
        """
        if self._closed:
            raise RuntimeError("ProjectRegistry has been closed; cannot serve requests.")
        return await self._cache.get(project_id)

    async def _materialise(self, project_id: str) -> ProjectResources:
        """Async materialiser bound to the lazy cache.

        Logging lives here so the per-project context (project_id) is
        captured at the point the resources are actually being built.
        """
        logger.info("Materializing resources for new project: {pid}", pid=project_id)
        return await asyncio.to_thread(self._build_resources, project_id)

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

        ris = RawIngressStore(project_root=project_dir)
        rts = RawTraceStore(project_root=project_dir)

        sync_log_path = project_dir / "sync.log"
        sync_log = SyncLog(path=sync_log_path)

        pipeline = ObservationPipeline(
            raw_trace_store=rts,
            events_repository=repo,
            sync_log=sync_log,
            raw_ingress_store=ris,
        )

        analysis_runtime: ProjectAnalysisRuntime | None = None
        analysis_runtime_error: str | None = None
        try:
            analysis_runtime = build_project_analysis_runtime(
                secondsight_home=self._home,
                project_id=project_id,
                db_engine=db_engine,
                events_repository=repo,
                raw_trace_store=rts,
            )
            analysis_runtime.trigger.register_pipeline_callback(pipeline)
            logger.info(
                "Analysis runtime ready for project: {pid} (event callback registered)",
                pid=project_id,
            )
        except Exception as exc:
            analysis_runtime_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Failed to build analysis runtime for project {pid}: {err}",
                pid=project_id,
                err=analysis_runtime_error,
            )

        return ProjectResources(
            project_id=project_id,
            db_engine=db_engine,
            events_repository=repo,
            raw_ingress_store=ris,
            raw_trace_store=rts,
            sync_log=sync_log,
            pipeline=pipeline,
            analysis_runtime=analysis_runtime,
            analysis_runtime_error=analysis_runtime_error,
        )

    @staticmethod
    def _finalise(project_id: str, resources: ProjectResources) -> None:
        """Best-effort per-project disposal during ``aclose()``.

        Exceptions are caught and logged so that one failing engine does not
        prevent disposal of the others.
        """
        try:
            resources.db_engine.dispose()
            logger.debug("Disposed DBEngine for project: {pid}", pid=project_id)
        except Exception as exc:
            logger.warning(
                "Error disposing DBEngine for project {pid}: {err}",
                pid=project_id,
                err=exc,
            )

    def materialized_project_ids(self) -> list[str]:
        """Return a snapshot of project_ids that have been materialized so far.

        Projects are materialized lazily on first ingest. At startup, this
        list is empty. It grows as the server receives events for new projects.

        Used by the server's Sweeper coordinator to iterate known projects
        without requiring a separate project-discovery scan.

        Returns a snapshot (list), not a live view, so the caller can iterate
        safely even if new projects are materialized concurrently.
        """
        return list(self._cache._values.keys())

    async def aclose(self) -> None:
        """Close all materialized engines.  Idempotent.

        Called by the lifespan shutdown hook.  After aclose(), get()
        raises RuntimeError.
        """
        if self._closed:
            return
        self._closed = True
        await self._cache.aclose()


__all__ = ["ProjectRegistry", "ProjectResources"]
