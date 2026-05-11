"""Effectiveness tracking for conventions (GUR-105, P3A-5).

Per aggregation cycle, measures whether a convention's source flag type
frequency is decreasing (effective) or not (ineffective). Uses the
behavior_flags table to compute frequency across analysis windows.

Judgment criteria (SD §5.9.4):
    - EFFECTIVE: flag frequency decreased ≥ 30% compared to pre-convention baseline
    - INEFFECTIVE: flag frequency unchanged or increased after ≥ 2 cycles
    - INSUFFICIENT_DATA: fewer than 2 post-convention analysis cycles available

The effectiveness judgment feeds into the lifecycle state machine:
    - INEFFECTIVE conventions are candidates for → obsolete transition
    - EFFECTIVE conventions stay active (no action needed)
    - INSUFFICIENT_DATA means no lifecycle action yet

Silent failure conditions:
    - If the convention's source_flag_type is None (e.g., manually created),
      effectiveness cannot be measured → always INSUFFICIENT_DATA.
    - If the behavior_flags table has no rows for the project, all conventions
      report INSUFFICIENT_DATA. This is correct for fresh projects.
    - If a convention targets a flag_type that no longer exists in the enum,
      the query returns 0 flags → frequency drops to 0 → EFFECTIVE.
      Acceptable: a deprecated flag type means the behavior it tracked
      no longer occurs.

Design assumptions:
    - Frequency = count of flags of that type per session, averaged over
      the measurement window.
    - Pre-convention baseline: average frequency in sessions BEFORE the
      convention's created_at.
    - Post-convention measurement: average frequency in sessions AFTER
      the convention's created_at.
    - At least 2 post-convention sessions required for judgment.

Ref: SD §5.9.4
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

import sqlalchemy as sa

if TYPE_CHECKING:
    from secondsight.analysis.schemas import Directive
    from secondsight.storage.db_engine import DBEngine


class EffectivenessJudgment(str, Enum):
    """Result of effectiveness evaluation for a convention."""

    EFFECTIVE = "effective"
    INEFFECTIVE = "ineffective"
    INSUFFICIENT_DATA = "insufficient_data"


_EFFECTIVENESS_THRESHOLD = 0.30


@dataclass(frozen=True, slots=True)
class EffectivenessResult:
    """Effectiveness measurement for a single convention."""

    directive_id: str
    judgment: EffectivenessJudgment
    baseline_frequency: float | None
    current_frequency: float | None
    change_ratio: float | None
    post_convention_sessions: int


def measure_effectiveness(
    directive: "Directive",
    db_engine: "DBEngine",
) -> EffectivenessResult:
    """Measure a convention's effectiveness based on flag frequency change.

    Queries the behavior_flags table for the convention's source_flag_type,
    comparing pre-convention and post-convention frequencies.
    """
    from secondsight.storage.behavior_flags_table import (
        behavior_flags,
    )

    if not directive.source_flag_type:
        return EffectivenessResult(
            directive_id=directive.id,
            judgment=EffectivenessJudgment.INSUFFICIENT_DATA,
            baseline_frequency=None,
            current_frequency=None,
            change_ratio=None,
            post_convention_sessions=0,
        )

    convention_created = directive.created_at

    with db_engine.engine.connect() as conn:
        baseline = _compute_frequency(
            conn, behavior_flags, directive.project_id,
            directive.source_flag_type, before=convention_created,
        )
        current = _compute_frequency(
            conn, behavior_flags, directive.project_id,
            directive.source_flag_type, after=convention_created,
        )
        post_sessions = _count_distinct_sessions(
            conn, behavior_flags, directive.project_id,
            after=convention_created,
        )

    if post_sessions < 2 or baseline is None:
        return EffectivenessResult(
            directive_id=directive.id,
            judgment=EffectivenessJudgment.INSUFFICIENT_DATA,
            baseline_frequency=baseline,
            current_frequency=current,
            change_ratio=None,
            post_convention_sessions=post_sessions,
        )

    if baseline == 0:
        change_ratio = 0.0 if (current or 0) == 0 else 1.0
    else:
        change_ratio = ((baseline - (current or 0)) / baseline)

    if change_ratio >= _EFFECTIVENESS_THRESHOLD:
        judgment = EffectivenessJudgment.EFFECTIVE
    else:
        judgment = EffectivenessJudgment.INEFFECTIVE

    return EffectivenessResult(
        directive_id=directive.id,
        judgment=judgment,
        baseline_frequency=baseline,
        current_frequency=current,
        change_ratio=round(change_ratio, 4),
        post_convention_sessions=post_sessions,
    )


def _compute_frequency(
    conn: sa.Connection,
    table: sa.Table,
    project_id: str,
    flag_type: str,
    *,
    before: datetime | None = None,
    after: datetime | None = None,
) -> float | None:
    """Average flags-per-session for a given flag_type within a time window.

    Returns None if no sessions exist in the window.
    """
    where_clauses = [
        table.c.project_id == project_id,
        table.c.flag_type == flag_type,
    ]
    if before is not None:
        where_clauses.append(table.c.created_at < before)
    if after is not None:
        where_clauses.append(table.c.created_at >= after)

    stmt = sa.select(
        sa.func.count().label("total_flags"),
        sa.func.count(sa.distinct(table.c.session_id)).label("session_count"),
    ).where(*where_clauses)

    row = conn.execute(stmt).first()
    if row is None or row.session_count == 0:
        return None
    return row.total_flags / row.session_count


def _count_distinct_sessions(
    conn: sa.Connection,
    table: sa.Table,
    project_id: str,
    *,
    after: datetime | None = None,
) -> int:
    """Count distinct sessions with any flags after a given time."""
    where_clauses = [table.c.project_id == project_id]
    if after is not None:
        where_clauses.append(table.c.created_at >= after)

    stmt = sa.select(
        sa.func.count(sa.distinct(table.c.session_id))
    ).where(*where_clauses)

    result = conn.execute(stmt).scalar()
    return result or 0


__all__ = [
    "EffectivenessJudgment",
    "EffectivenessResult",
    "measure_effectiveness",
]
