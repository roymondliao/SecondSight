"""ObservationPipeline — encodes the durability contract (P1-4).

Contract (asymmetric error handling):
    1. Filesystem write FIRST. If it fails, raise — the caller sees it.
    2. DB INSERT next. If it fails, log to sync_log and continue. The
       raw trace is already safe; backfill (P1-13) recovers it later.
    3. If sync_log itself fails, the recovery path is gone — surface
       this as CRITICAL and re-raise. Losing both DB and the recovery
       hook silently is the worst possible failure.

This file is intentionally short. The contract IS the implementation.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from secondsight.event import Event
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.sync_log import SyncLog


class ObservationPipeline:
    """Orchestrates filesystem-first event durability."""

    def __init__(
        self,
        raw_trace_store: RawTraceStore,
        events_repository: EventsRepository,
        sync_log: SyncLog,
    ) -> None:
        self._rts = raw_trace_store
        self._repo = events_repository
        self._sync_log = sync_log

    async def ingest(self, event: Event) -> None:
        """See module docstring for the durability contract."""
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
