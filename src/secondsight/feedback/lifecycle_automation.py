"""Automated directive lifecycle transitions (GUR-108, P3B-2 + P3B-3).

Called by the orchestrator after aggregation to enforce TTL expiry and
detect re-activation candidates.

P3B-2 — Expiry enforcement:
    Scans active conventions whose ``expires_at`` is in the past.
    Transitions them to ``expired`` status. The ``get_active_conventions``
    query already filters these out defensively, but the status transition
    makes the expiry visible in the API and dashboard.

P3B-3 — Re-activation:
    Scans ``obsolete`` conventions whose source flag type has rebounded
    (flag frequency is non-zero in recent sessions). Transitions them
    back to ``active``. This prevents permanent loss of a convention
    that was marked obsolete due to a temporary dip in flag frequency.

Both operations are idempotent — running them multiple times produces
the same DB state.

Silent failure conditions:
    - If ``expires_at`` is NULL for all conventions, expiry enforcement
      is a no-op. Correct: conventions without TTL never expire.
    - If no conventions are ``obsolete``, re-activation is a no-op.
    - If the behavior_flags table has no recent rows for a flag type,
      the rebound check returns 0 and no re-activation occurs.

Design assumptions:
    - Expiry enforcement is clock-based (UTC now vs. expires_at).
    - Re-activation threshold: ≥ 1 flag of the source type in the
      last REACTIVATION_LOOKBACK_DAYS (default 14) days.
    - Both operations use the lifecycle state machine for transition
      validation (loud failure on invalid transitions).

Ref: SD §5.9.2, §5.9.3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

import sqlalchemy as sa

from secondsight.analysis.schemas import DirectiveStatus, DirectiveType
from secondsight.feedback.lifecycle import validate_transition

if TYPE_CHECKING:
    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.directives_repository import DirectivesRepository

_logger = logging.getLogger(__name__)

REACTIVATION_LOOKBACK_DAYS: Final[int] = 14


@dataclass(frozen=True, slots=True)
class LifecycleAutomationResult:
    """Outcome of one lifecycle automation run."""

    expired_count: int
    reactivated_count: int


def enforce_expiry(
    project_id: str,
    directives_repo: "DirectivesRepository",
) -> int:
    """Transition active conventions past their expires_at to expired.

    Uses atomic conditional UPDATE (WHERE status='active') to avoid
    TOCTOU: if another process transitions the directive between our
    SELECT and UPDATE, the UPDATE becomes a no-op (rowcount=0) rather
    than corrupting the lifecycle graph.

    Returns the number of conventions expired in this run.
    """
    now = datetime.now(tz=timezone.utc)
    from secondsight.storage.directives_table import directives

    with directives_repo._db.engine.connect() as conn:
        stmt = sa.select(directives).where(
            sa.and_(
                directives.c.project_id == project_id,
                directives.c.type == DirectiveType.CONVENTION.value,
                directives.c.status == DirectiveStatus.ACTIVE.value,
                directives.c.expires_at.is_not(None),
                directives.c.expires_at <= now,
            ),
        )
        expired_rows = list(conn.execute(stmt).mappings())

    count = 0
    for row in expired_rows:
        directive_id = row["id"]
        try:
            validate_transition(
                DirectiveStatus.ACTIVE,
                DirectiveStatus.EXPIRED,
                directive_id=directive_id,
            )
            with directives_repo._db.engine.begin() as conn:
                result = conn.execute(
                    sa.update(directives)
                    .where(
                        sa.and_(
                            directives.c.id == directive_id,
                            directives.c.status == DirectiveStatus.ACTIVE.value,
                        ),
                    )
                    .values(
                        status=DirectiveStatus.EXPIRED.value,
                        disabled_at=None,
                        disabled_reason=None,
                        updated_at=now,
                    ),
                )
                if result.rowcount == 0:
                    _logger.debug(
                        "expiry_enforcement: directive_id=%r no longer active, skipped",
                        directive_id,
                    )
                    continue
            count += 1
            _logger.info(
                "expiry_enforcement: expired directive_id=%r (expires_at=%s, now=%s)",
                directive_id,
                row["expires_at"],
                now.isoformat(),
            )
        except Exception as exc:
            _logger.warning(
                "expiry_enforcement: failed to expire directive_id=%r: %s",
                directive_id,
                exc,
            )
    return count


def enforce_reactivation(
    project_id: str,
    directives_repo: "DirectivesRepository",
    db_engine: "DBEngine",
    *,
    lookback_days: int = REACTIVATION_LOOKBACK_DAYS,
) -> int:
    """Re-activate obsolete conventions whose flag type has rebounded.

    Scans obsolete conventions, checks if the source_flag_type has
    any flags in the last ``lookback_days`` days. If so, transitions
    the convention back to active.

    Uses atomic conditional UPDATE (WHERE status='obsolete') to avoid
    TOCTOU races with concurrent status changes.

    Returns the number of conventions re-activated in this run.
    """
    from secondsight.storage.behavior_flags_table import behavior_flags
    from secondsight.storage.directives_table import directives

    with directives_repo._db.engine.connect() as conn:
        stmt = sa.select(directives).where(
            sa.and_(
                directives.c.project_id == project_id,
                directives.c.type == DirectiveType.CONVENTION.value,
                directives.c.status == DirectiveStatus.OBSOLETE.value,
            ),
        )
        obsolete_rows = list(conn.execute(stmt).mappings())

    if not obsolete_rows:
        return 0

    threshold = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    now = datetime.now(tz=timezone.utc)

    count = 0
    for row in obsolete_rows:
        flag_type = row["source_flag_type"]
        if not flag_type:
            continue

        with db_engine.engine.connect() as conn:
            recent_count_result = conn.execute(
                sa.select(sa.func.count())
                .select_from(behavior_flags)
                .where(
                    sa.and_(
                        behavior_flags.c.project_id == project_id,
                        behavior_flags.c.flag_type == flag_type,
                        behavior_flags.c.created_at >= threshold,
                    ),
                ),
            ).scalar()

        recent_count = recent_count_result or 0
        if recent_count == 0:
            continue

        directive_id = row["id"]
        try:
            validate_transition(
                DirectiveStatus.OBSOLETE,
                DirectiveStatus.ACTIVE,
                directive_id=directive_id,
            )
            with directives_repo._db.engine.begin() as conn:
                result = conn.execute(
                    sa.update(directives)
                    .where(
                        sa.and_(
                            directives.c.id == directive_id,
                            directives.c.status == DirectiveStatus.OBSOLETE.value,
                        ),
                    )
                    .values(
                        status=DirectiveStatus.ACTIVE.value,
                        disabled_at=None,
                        disabled_reason=None,
                        updated_at=now,
                    ),
                )
                if result.rowcount == 0:
                    _logger.debug(
                        "reactivation: directive_id=%r no longer obsolete, skipped",
                        directive_id,
                    )
                    continue
            count += 1
            _logger.info(
                "reactivation: re-activated directive_id=%r "
                "(flag_type=%s, recent_flags=%d in last %d days)",
                directive_id,
                flag_type,
                recent_count,
                lookback_days,
            )
        except Exception as exc:
            _logger.warning(
                "reactivation: failed to re-activate directive_id=%r: %s",
                directive_id,
                exc,
            )
    return count


def run_lifecycle_automation(
    project_id: str,
    directives_repo: "DirectivesRepository",
    db_engine: "DBEngine",
) -> LifecycleAutomationResult:
    """Run both expiry enforcement and re-activation for a project.

    Called by the orchestrator after aggregation completes.
    """
    expired = enforce_expiry(project_id, directives_repo)
    reactivated = enforce_reactivation(project_id, directives_repo, db_engine)
    return LifecycleAutomationResult(
        expired_count=expired,
        reactivated_count=reactivated,
    )


__all__ = [
    "LifecycleAutomationResult",
    "REACTIVATION_LOOKBACK_DAYS",
    "enforce_expiry",
    "enforce_reactivation",
    "run_lifecycle_automation",
]
