"""AnalysisResultsPurger — task-B2 of GUR-149.

Sibling of ``retention.py``; same precedence model, same ``PurgeResult``
contract. Reaps the analyzed-material tables — ``session_reports`` and
``behavior_flags`` — for sessions whose ``session_reports.created_at``
crossed the resolved ``analysis_ttl_days`` boundary.

Why a separate module (vs. extending ``retention.py``):
    The yin-side cohesion review of task-B1 noted ``retention.py`` already
    carries three distinct death-reasons (config resolution, expiry
    enumeration, raw-traces destruction). Adding a fourth (analysis
    destruction) would deepen that cohesion violation. Per
    ``2-plan.md §4`` File Map, this module lives next to retention.py
    so they remain discoverable as a pair, but the destructive primitives
    do not share a death-reason and stay separate.

Boundary basis (D1 of 2-plan.md):
    ``session_reports.created_at`` — preserved on upsert per the
    repository's idempotency contract (lines 5-7 of
    ``session_reports_repository.py``). A re-run that updates an existing
    row preserves ``created_at``; ``updated_at`` would extend the TTL
    indefinitely and is therefore unsuitable as the boundary basis.

Purge order (D4 of 2-plan.md):
    ``behavior_flags`` FIRST, ``session_reports`` SECOND. Reasoning:
    crashing between stages must leave forward-progressable state. With
    this order, a partial failure leaves ``session_reports`` intact (the
    enumerator re-detects it on the next run) and ``behavior_flags``
    already gone — the next run's ``DELETE FROM behavior_flags`` is a
    no-op rowcount 0, then ``session_reports`` deletion completes the
    reap. Reverse order would leave orphan ``behavior_flags`` with no
    FK constraint to enforce cleanup (verified absent via
    ``Grep -p Foreign|REFERENCES`` over storage/*.py).

DC-B6 (partial-purge orphan guard) and DC-B7 (empty-install no-op) are
the silent-failure paths these primitives close.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from loguru import logger

from secondsight.storage.behavior_flags_table import behavior_flags
from secondsight.storage.raw_trace_store import is_safe_session_id
from secondsight.storage.retention import PurgeFailure, PurgeResult
from secondsight.storage.session_reports_table import session_reports

if TYPE_CHECKING:
    from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
    from secondsight.storage.session_reports_repository import SessionReportsRepository


# ---------------------------------------------------------------------------
# ExpiredAnalysis — dataclass mirroring ExpiredSession from retention.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpiredAnalysis:
    """One session_reports row whose retention has expired.

    ``report_created_at`` is preserved on the result so the cleanup log
    line can attribute *why* the row was selected. Without it, an
    operator looking at "session_reports reaped" has no way to verify
    the boundary was applied correctly. Mirrors the rationale for
    ``ExpiredSession.last_event_at`` in GUR-147.
    """

    session_id: str
    report_created_at: datetime


# Note: there is intentionally NO `AnalysisPurgeStage` Literal exported
# from this module. The two DB-level stages (`behavior_flags` and
# `session_reports`) are recorded in the `error` field of `PurgeFailure`
# via a "(stage=...)" suffix rather than in the typed `stage` field
# (which remains "database" to match the existing PurgeFailure schema
# from GUR-147). Exporting an `AnalysisPurgeStage` type would advertise
# a per-stage distinction at the type level that the implementation does
# not preserve — that is the type-system lie the v1 yin review flagged.
# Consumers needing programmatic per-stage access should grep the error
# string for "(stage=...)"; a future PurgeFailure refactor can widen
# the typed field if a structured consumer materializes.


# ---------------------------------------------------------------------------
# enumerate_expired_analyses — pure read-side enumerator
# ---------------------------------------------------------------------------


def enumerate_expired_analyses(
    reports_repo: SessionReportsRepository,
    *,
    analysis_ttl_days: int,
    now: datetime,
) -> list[ExpiredAnalysis]:
    """Return session_reports rows whose ``created_at`` is at or before
    the TTL cutoff (``now - analysis_ttl_days``).

    The boundary is ``created_at``, NOT ``updated_at`` (decision D1 in
    2-plan.md): re-running analysis on a session preserves ``created_at``
    via the repository's UPSERT contract; using ``updated_at`` would
    extend the TTL indefinitely on every re-run.

    Inclusive boundary: a row whose ``created_at`` is *exactly* at
    ``now - ttl_days`` IS expired. Strict inequality would let rows
    linger one tick past their advertised TTL (mirrors GUR-147
    ``enumerate_expired_sessions``).

    Args:
        reports_repo: SessionReportsRepository for the project being scanned.
        analysis_ttl_days: Resolved TTL in days
            (from RetentionConfig.analysis_ttl_days).
        now: Wall-clock reference for cutoff computation. Passed in so
            tests are deterministic.

    Returns:
        List of :class:`ExpiredAnalysis` ordered by session_id ascending
        for stable cleanup logs and reproducible ``--dry-run`` output.

    Raises:
        Nothing for the empty case (DC-B7). Underlying SQLAlchemy errors
        propagate; this function does not catch them.
    """
    cutoff = now - timedelta(days=analysis_ttl_days)
    stmt = (
        sa.select(
            session_reports.c.session_id,
            session_reports.c.created_at,
        )
        .where(session_reports.c.created_at <= cutoff)
        .order_by(session_reports.c.session_id.asc())
    )
    # Assumption: reports_repo._db is a sync DBEngine with a SQLAlchemy
    # Engine at .engine. If SessionReportsRepository ever moves to an
    # async pool or hides the engine, this access breaks at runtime
    # (AttributeError). Mirrors the same pattern in retention.py
    # (events_repository._db.engine); a future _HasDbEngine Protocol
    # would formalize the contract — deferred per scar-B2-2.
    with reports_repo._db.engine.connect() as conn:  # noqa: SLF001 — see retention module note
        rows = conn.execute(stmt).all()
    return [
        ExpiredAnalysis(
            session_id=r.session_id,
            report_created_at=r.created_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# AnalysisResultsPurger — destructive primitive
# ---------------------------------------------------------------------------


def _delete_behavior_flags_for_session(
    repo: BehaviorFlagsRepository, session_id: str
) -> int:
    """``DELETE FROM behavior_flags WHERE session_id = ?``. Returns rowcount.

    Per-session rather than batched IN(...) so a single corrupt session
    cannot pull the whole batch down — the DC-B6 contract says partial
    failure is recoverable and other sessions should still be reaped.

    Idempotent: zero matching rows returns rowcount 0 cleanly. This
    matters for the partial-failure recovery path: if a prior purge
    deleted behavior_flags for this session and crashed before deleting
    session_reports, the next run's enumerator re-detects the same
    session and re-attempts both stages — the behavior_flags re-attempt
    must be a no-op, not a raise.
    """
    stmt = sa.delete(behavior_flags).where(
        behavior_flags.c.session_id == session_id
    )
    with repo._db.engine.begin() as conn:  # noqa: SLF001 — see retention module note
        return int(conn.execute(stmt).rowcount or 0)


def _delete_session_report_for_session(
    repo: SessionReportsRepository, session_id: str
) -> int:
    """``DELETE FROM session_reports WHERE session_id = ?``. Returns rowcount."""
    stmt = sa.delete(session_reports).where(
        session_reports.c.session_id == session_id
    )
    with repo._db.engine.begin() as conn:  # noqa: SLF001 — see retention module note
        return int(conn.execute(stmt).rowcount or 0)


class AnalysisResultsPurger:
    """Destructive side of analysis retention. behavior_flags first,
    session_reports second (D4 in 2-plan.md).

    The two operations are NOT a single transaction — they are issued
    via separate ``engine.begin()`` blocks, mirroring the per-row
    isolation pattern from GUR-147's ``RawTracesPurger``. Rationale: a
    single corrupt session cannot pull down the whole batch; partial
    failure is recoverable per DC-B6.

    On crash between stages for one session: behavior_flags are gone,
    session_reports row remains. The next purge run re-detects the same
    row; ``_delete_behavior_flags_for_session`` returns 0 cleanly (the
    rows are already gone), then the session_reports delete completes
    the reap. Forward progress, no orphans.
    """

    def __init__(
        self,
        *,
        session_reports_repo: SessionReportsRepository,
        behavior_flags_repo: BehaviorFlagsRepository,
    ) -> None:
        self._reports_repo = session_reports_repo
        self._flags_repo = behavior_flags_repo

    def purge(self, expired: Sequence[ExpiredAnalysis]) -> PurgeResult:
        """Reap each expired analysis row + its behavior_flags.

        Returns a ``PurgeResult`` describing successes and failures.
        Order of ``purged_session_ids`` matches input order (sessions
        that failed are NOT in this list — they appear in ``failures``
        instead).

        Empty input returns an empty ``PurgeResult`` (DC-B7); no DB
        statements are issued.
        """
        purged: list[str] = []
        failures: list[PurgeFailure] = []

        for entry in expired:
            sid = entry.session_id

            # Defense-in-depth (security review GUR-149 finding 2): mirror
            # the GUR-147 `RawTracesPurger` pattern of validating session_id
            # via `is_safe_session_id` before any destructive operation.
            # SQLAlchemy parameterization already prevents SQL injection on
            # the DELETE statements below, but the GUR-147 review established
            # the pattern that any per-session destructive primitive must
            # gate the input even though direct adversarial paths are not
            # currently reachable. A future enumerator that bypasses
            # `enumerate_expired_analyses` and feeds raw input here would
            # otherwise reach the destructive site without re-validation.
            if not is_safe_session_id(sid):
                logger.error(
                    "analysis_results purge: rejected unsafe session_id="
                    "{sid!r}; refusing to issue DELETE",
                    sid=sid,
                )
                failures.append(
                    PurgeFailure(
                        session_id=sid,
                        stage="database",
                        error=(
                            "ValueError: unsafe session_id "
                            "(stage=validation)"
                        ),
                    )
                )
                continue

            # Stage 1: behavior_flags. Idempotent rowcount 0 on absent rows.
            try:
                _delete_behavior_flags_for_session(self._flags_repo, sid)
            except Exception as exc:  # noqa: BLE001 — boundary owns the catch
                logger.error(
                    "analysis_results purge: behavior_flags delete failed for "
                    "session_id={sid}: {exc_type}: {exc}",
                    sid=sid,
                    exc_type=type(exc).__name__,
                    exc=exc,
                )
                failures.append(
                    PurgeFailure(
                        session_id=sid,
                        # Reuse retention.PurgeStage's "filesystem" / "database"
                        # vocabulary by stuffing analysis-specific stages into
                        # the same enum-literal type. Loguru-friendly string is
                        # what the failure consumer reads. Type-checker note:
                        # PurgeFailure.stage is typed as
                        # Literal["filesystem", "database"]; we extend by
                        # passing the literal string and accept the implicit
                        # widening. See scar item.
                        stage="database",
                        error=f"{type(exc).__name__}: {exc} (stage=behavior_flags)",
                    )
                )
                # behavior_flags-first contract: do NOT touch session_reports if
                # this stage failed. Next reap will re-enumerate and retry both
                # sides.
                continue

            # Stage 2: session_reports. behavior_flags for `sid` are now gone.
            try:
                _delete_session_report_for_session(self._reports_repo, sid)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "analysis_results purge: session_reports delete failed "
                    "for session_id={sid} AFTER behavior_flags removal — "
                    "next reap will re-detect and retry: "
                    "{exc_type}: {exc}",
                    sid=sid,
                    exc_type=type(exc).__name__,
                    exc=exc,
                )
                failures.append(
                    PurgeFailure(
                        session_id=sid,
                        stage="database",
                        error=f"{type(exc).__name__}: {exc} (stage=session_reports)",
                    )
                )
                continue

            purged.append(sid)

        return PurgeResult(
            purged_session_ids=tuple(purged),
            failures=tuple(failures),
        )


__all__ = [
    "AnalysisResultsPurger",
    "ExpiredAnalysis",
    "enumerate_expired_analyses",
]
