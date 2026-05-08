"""PostAnalysisCleanupTrigger — task-B4 of GUR-149.

The canonical consumer of ``Orchestrator.on_analysis_complete``. When
the operator opts into ``[retention].cleanup_after_analysis = true``,
this trigger fires immediately after each ``analyze_session`` reaches
``summary_written`` and asks GUR-147's ``RawTracesPurger.purge()`` to
reap raw traces for that one session.

Construction is at boot time (CLI / app factory, task-B6); the
orchestrator only knows the callback shape (``Callable[[str], None]``).

LOAD-BEARING NOTE (gap-fs-collision, 2-plan.md D5):
    When this trigger fires, ``RawTracesPurger.purge()`` shutil.rmtree's
    the entire ``{home}/projects/{project_id}/sessions/{session_id}/``
    directory — which **INCLUDES** the orchestrator's
    ``session_report.json`` FS backup (see
    ``orchestrator.py:_write_filesystem_backup``). This is the accepted
    gap from planning: the DB row in ``session_reports`` remains
    authoritative, and tools that consume the FS backup must fall back
    to the DB after eager cleanup. The structured INFO log line below
    discloses this side effect explicitly so an operator reading
    cleanup logs can correlate the two effects.

Failure policy (DC-B5 + interaction with DC-B3):
    The orchestrator's ``_invoke_on_analysis_complete`` swallows ALL
    callback exceptions (DC-B3, task-B3). The trigger could theoretically
    rely on that swallow and propagate purger failures — but doing so
    would produce two ERROR records (one from the purger, one from the
    orchestrator boundary) for one root cause. To keep the failure trail
    readable, the trigger logs WARNING for ``PurgeResult.had_failures``
    and returns normally. The purger's own structured ERROR logs name
    the session and stage; this trigger's WARNING points operators at
    those logs without duplicating the diagnostic.

DC-B5 idempotency: relies on GUR-147's ``_delete_fs_session`` returning
``False`` (not raising) on absent dirs and
``_delete_db_events_for_session`` returning rowcount 0 cleanly. The
trigger is therefore safe to invoke twice, or after a CLI cleanup
already reaped the session.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from secondsight.storage.retention import ExpiredSession, PurgeResult

if TYPE_CHECKING:
    from secondsight.storage.events_repository import EventsRepository

_logger = logging.getLogger(__name__)


class _PurgerProtocol(Protocol):
    """Structural shape the trigger requires from its purger dependency.

    Quality review B4 Important fix: the trigger only needs the
    ``.purge(list[ExpiredSession]) -> PurgeResult`` method shape, not the
    full ``RawTracesPurger`` concrete class. Annotating against
    ``RawTracesPurger`` directly forced test spies (``SpyPurger``,
    ``FailingPurger``) to carry ``# type: ignore[arg-type]`` — the type
    system was claiming more than the runtime requirement. This Protocol
    makes the structural requirement explicit; the runtime behavior is
    unchanged.
    """

    def purge(self, expired: Sequence[ExpiredSession]) -> PurgeResult: ...


class PostAnalysisCleanupTrigger:
    """Bridge from ``Orchestrator.on_analysis_complete`` to GUR-147's
    ``RawTracesPurger.purge()``.

    Constructed at boot time. Registered as the orchestrator's callback
    when ``[retention].cleanup_after_analysis = true``.

    Single-purpose: convert one ``session_id`` into one purge call.
    """

    def __init__(
        self,
        *,
        cleanup_after_analysis: bool,
        raw_traces_purger: _PurgerProtocol,
        events_repo: EventsRepository,
    ) -> None:
        self._cleanup_after_analysis = cleanup_after_analysis
        self._raw_traces_purger = raw_traces_purger
        self._events_repo = events_repo

    def __call__(self, session_id: str) -> None:
        """Invoke the eager-cleanup path for one just-completed session.

        Steps:
          1. Disabled-path early return: log INFO, return.
          2. Look up the session's last_event_at via existing
             ``EventsRepository.get_session_events`` API. If empty,
             log INFO and return (DC-B5: idempotent on already-reaped
             or non-existent sessions).
          3. Synthesize ``ExpiredSession(session_id, last_event_at)``
             — the ``last_event_at`` field is preserved on the dataclass
             for log attribution (per GUR-147 retention.py contract).
          4. Call ``raw_traces_purger.purge([expired])``.
          5. Log INFO on success (with gap-fs-collision disclosure)
             OR WARNING on ``had_failures`` (without re-raising).

        Exception contract (yin review B4 Important fix — explicit
        per-method labeling):

        - Purge-failure path (``PurgeResult.had_failures=True``): handled
          locally; emits WARNING; returns normally. This guarantee is
          the trigger's OWN logic and survives any change to the
          orchestrator's exception boundary.

        - Lookup-failure path (``get_session_events`` raises) AND
          unexpected-purger-failure path (``purge()`` raises outside its
          per-row try/except): the trigger does NOT catch these. They
          propagate to the orchestrator's ``_invoke_on_analysis_complete``
          DC-B3 swallow boundary (``orchestrator.py:_invoke_on_analysis_complete``).
          The trigger relies on that boundary for the unconditional
          "Does NOT raise" guarantee. If a future change tightens the
          orchestrator's swallow, the trigger's contract narrows
          accordingly — to "Does NOT raise on purge-reported failures."
          This delegated reliance is documented HERE so a reader of just
          this method does not over-trust the unconditional phrasing.
        """
        if not self._cleanup_after_analysis:
            _logger.info(
                "post_analysis_cleanup: skipped session_id=%r "
                "(cleanup_after_analysis=False)",
                session_id,
            )
            return

        events = self._events_repo.get_session_events(session_id)
        if not events:
            # DC-B5 idempotency path: session already reaped, or never
            # existed. Either way, nothing to purge. Log + return.
            _logger.info(
                "post_analysis_cleanup: skipped session_id=%r "
                "(no events found — already reaped or never existed)",
                session_id,
            )
            return

        # max() is safe here: the `if not events:` guard above guarantees
        # the iterable is non-empty. A future edit that loosens the guard
        # must also handle the empty-sequence ValueError.
        last_event_at = max(e.timestamp for e in events)
        expired = ExpiredSession(
            session_id=session_id,
            last_event_at=last_event_at,
        )

        result = self._raw_traces_purger.purge([expired])

        if result.had_failures:
            # Purger has already emitted structured ERROR logs naming
            # session_id + stage. Surface a WARNING that points there
            # without duplicating the diagnostic detail.
            _logger.warning(
                "post_analysis_cleanup: purge had failures for "
                "session_id=%r — see RawTracesPurger ERROR logs for "
                "stage-level detail",
                session_id,
            )
            return

        # Success — disclose the FS-collision side effect (gap-fs-collision,
        # 2-plan.md D5). Operators reading cleanup logs need to know that
        # the session_report.json FS backup is gone alongside the events
        # directory; the DB row in session_reports remains authoritative.
        _logger.info(
            "post_analysis_cleanup: eagerly purged session_id=%r "
            "(last_event_at=%s) — note: FS session_report.json backup "
            "also removed; DB row remains in session_reports",
            session_id,
            last_event_at.isoformat(),
        )


__all__ = [
    "PostAnalysisCleanupTrigger",
]
