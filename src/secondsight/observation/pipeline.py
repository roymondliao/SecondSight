"""ObservationPipeline — encodes the durability contract (P1-4).

Contract (asymmetric error handling):
    1. Filesystem write FIRST. If it fails, raise — the caller sees it.
    2. DB INSERT next. If it fails, log to sync_log and continue. The
       raw trace is already safe; backfill (P1-13) recovers it later.
    3. If sync_log itself fails, the recovery path is gone — surface
       this as CRITICAL and re-raise. Losing both DB and the recovery
       hook silently is the worst possible failure.
    4. Post-ingest callbacks fire AFTER a successful DB write (or after
       the DB failure is recorded). Callback exceptions are caught and
       logged; they NEVER break the ingest contract. Callbacks are
       scheduled via asyncio.create_task so ingest() itself is unblocked.

Post-ingest callback contract (GUR-103 P2-15):
    - Callbacks are registered via add_post_ingest_callback(cb).
    - cb is an async callable: async def cb(event: Event) -> None.
    - Each callback is scheduled as asyncio.create_task(cb(event)).
      If create_task fails (e.g., event loop closing), the error is
      logged and ingest() continues.
    - Callback tasks run in the background. Exceptions inside them are
      logged by a done_callback; they do NOT surface to ingest()'s caller.
    - The callback list is per-instance (not class-level) to avoid
      cross-instance contamination in tests.

This file is intentionally short. The contract IS the implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable

from loguru import logger
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from secondsight.event import Event
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.ingress_record import IngressRecord
from secondsight.storage.raw_ingress_store import RawIngressStore
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.sync_log import SyncLog


class ObservationPipeline:
    """Orchestrates filesystem-first event durability.

    Post-ingest callbacks:
        Registered via add_post_ingest_callback(cb). Each cb is an async
        callable (async def cb(event: Event) -> None) that is scheduled
        as asyncio.create_task after each successful DB write. Callback
        exceptions are caught by a done_callback on the task; they never
        propagate to ingest()'s caller.

        Use case: GUR-103 Trigger registers a SESSION_END callback here to
        fire analysis dispatch without blocking the ingest path.
    """

    def __init__(
        self,
        raw_trace_store: RawTraceStore,
        events_repository: EventsRepository,
        sync_log: SyncLog,
        raw_ingress_store: RawIngressStore | None = None,
    ) -> None:
        self._rts = raw_trace_store
        self._repo = events_repository
        self._sync_log = sync_log
        self._ris = raw_ingress_store
        # Per-instance callback list. Not class-level to avoid cross-instance
        # contamination in tests. Type: list of async callables.
        self._post_ingest_callbacks: list[Callable[[Event], Awaitable[None]]] = []

    def add_post_ingest_callback(
        self, cb: Callable[[Event], Awaitable[None]]
    ) -> None:
        """Register an async post-ingest callback.

        cb will be called (via asyncio.create_task) after each event is
        successfully written to DB (or after the DB failure is recorded
        and the sync log is updated). Exceptions in cb are caught and
        logged; they do NOT affect ingest().

        Args:
            cb: async callable accepting a single Event argument.

        Note: callbacks fire on ALL events (not filtered here).
              The callback itself should filter by event_type if needed.
        """
        self._post_ingest_callbacks.append(cb)

    async def ingest(
        self,
        event: Event,
        *,
        ingress_record: IngressRecord | None = None,
    ) -> None:
        """See module docstring for the durability contract."""
        if ingress_record is not None and self._ris is not None:
            await self._ris.write(ingress_record)
        # Step 1: filesystem write — raises propagate to caller.
        raw_path = await self._rts.write(event)

        # Step 2: DB INSERT — best-effort.
        try:
            self._repo.insert(event)
        except (DBAPIError, SQLAlchemyError) as db_err:
            self._record_db_failure(event, raw_path, db_err)
        except KeyboardInterrupt:
            # Operator interrupt — propagate. Raw trace already safe.
            raise

        # Step 3: Fire post-ingest callbacks (GUR-103 P2-15).
        # Callbacks are non-blocking: each is scheduled as asyncio.create_task.
        # Exceptions inside tasks are caught by _make_callback_done_callback.
        # A failure to create_task (e.g., loop closing) is caught here and logged.
        self._fire_post_ingest_callbacks(event)

    def _fire_post_ingest_callbacks(self, event: Event) -> None:
        """Schedule all registered post-ingest callbacks as background tasks.

        Each callback is wrapped in asyncio.create_task. Exceptions inside
        the task are caught by a done_callback (not here) so they never
        surface to ingest()'s caller. If create_task itself fails (e.g.,
        event loop is closing), the error is caught and logged; ingest
        continues.
        """
        for cb in self._post_ingest_callbacks:
            try:
                task = asyncio.create_task(
                    cb(event),
                    name=f"post-ingest-cb-{event.id}",
                )
                task.add_done_callback(
                    lambda t, cb_name=getattr(cb, "__qualname__", repr(cb)): (
                        logger.error(
                            "post-ingest callback {name} raised: {exc}",
                            name=cb_name,
                            exc=t.exception(),
                        )
                        if not t.cancelled() and t.exception() is not None
                        else None
                    )
                )
            except RuntimeError as exc:
                logger.error(
                    "post-ingest callback create_task failed for event {id}: {err}",
                    id=event.id,
                    err=exc,
                )

    def _record_db_failure(
        self,
        event: Event,
        raw_trace_path,
        db_err: BaseException,
    ) -> None:
        try:
            self._sync_log.record_failure(event.id, raw_trace_path, db_err)
            logger.warning(
                "DB INSERT failed for event {id}; recorded to sync log: {err}",
                id=event.id,
                err=db_err,
            )
        except BaseException as log_err:
            logger.critical(
                "DB INSERT failed AND sync log write failed — "
                "recovery path is gone for event {id}: db_err={db}, log_err={log}",
                id=event.id,
                db=db_err,
                log=log_err,
            )
            raise
