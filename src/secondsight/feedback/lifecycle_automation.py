"""Automated directive lifecycle transitions (GUR-108, P3B-2).

Called by the orchestrator after aggregation to enforce TTL expiry.

P3B-2 — Expiry enforcement:
    Scans active conventions whose ``expires_at`` is in the past.
    Transitions them to ``expired`` status. The ``get_active_conventions``
    query already filters these out defensively, but the status transition
    makes the expiry visible in the API and dashboard.

Identity-based reactivation now lives in the aggregator's lineage resolution
path. The old source-flag-type rebound reactivation logic is intentionally
retired so there is only one auto-revival source of truth.

Silent failure conditions:
    - If ``expires_at`` is NULL for all conventions, expiry enforcement
      is a no-op. Correct: conventions without TTL never expire.
Design assumptions:
    - Expiry enforcement is clock-based (UTC now vs. expires_at).
    - TTL enforcement continues to use the lifecycle state machine for
      transition validation (loud failure on invalid transitions).

Ref: SD §5.9.2, §5.9.3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import sqlalchemy as sa

from secondsight.analysis.schemas import DirectiveStatus, DirectiveType
from secondsight.feedback.lifecycle import validate_transition

if TYPE_CHECKING:
    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.directives_repository import DirectivesRepository

_logger = logging.getLogger(__name__)


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
    lookback_days: int = 0,
) -> int:
    """Legacy shim: source-flag-type rebound reactivation is retired."""
    del project_id, directives_repo, db_engine, lookback_days
    return 0


def _enforce_capacity_ceiling(
    project_id: str,
    directives_repo: "DirectivesRepository",
    *,
    ceiling: int,
) -> int:
    """Transition lowest-weight active conventions to obsolete until bounded."""
    if ceiling <= 0:
        return 0
    active = directives_repo.list_active_for_capacity(project_id)
    if len(active) <= ceiling:
        return 0

    count = 0
    for directive in active[: len(active) - ceiling]:
        validate_transition(
            DirectiveStatus.ACTIVE,
            DirectiveStatus.OBSOLETE,
            directive_id=directive.id,
        )
        directives_repo.update_status(directive.id, DirectiveStatus.OBSOLETE)
        count += 1
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
    "enforce_expiry",
    "enforce_reactivation",
    "run_lifecycle_automation",
]
