"""DirectivesRepository — SQLAlchemy Core repository (GUR-100 task-3).

Lifecycle contract (per `project_directive_lifecycle_contract` memory):
- update_status(id, DISABLED, reason=...) requires non-None reason.
  Sets disabled_at=now(), disabled_reason=reason.
- update_status(id, <not-DISABLED>) requires reason=None. Clears
  disabled_at=NULL, disabled_reason=NULL.
- updated_at advances on every transition.
- update_status on a missing id raises LookupError (no silent no-op).

Defensive enum guard (D1): model_construct() bypasses Pydantic, so
insert() re-validates `status` and `type` against their enums. The
in-process surface is wrapped by GUR-104's HTTP PATCH endpoint, which
will further restrict user-PATCHable values to {active, disabled}.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_table import directives, metadata


class DirectivesRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create directives + indexes if absent. Idempotent."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def insert(self, directive: Directive) -> None:
        self._guard(directive)
        row = self._directive_to_row(directive)
        stmt = (
            sqlite_insert(directives)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

    def get_active_conventions(self, project_id: str) -> list[Directive]:
        """Active conventions only, sorted by frequency desc.

        NOTE: This method does NOT filter by `expires_at`. A convention
        whose `expires_at` is in the past but `status` is still 'active'
        WILL be returned. Expiry-checking + auto-transition to status
        'expired' is GUR-101's analyzer responsibility, not the
        repository's. See `acceptance.yaml` degradation scenario.
        """
        stmt = (
            sa.select(directives)
            .where(
                sa.and_(
                    directives.c.project_id == project_id,
                    directives.c.type == DirectiveType.CONVENTION.value,
                    directives.c.status == DirectiveStatus.ACTIVE.value,
                )
            )
            .order_by(directives.c.frequency.desc().nullslast())
        )
        with self._db.engine.connect() as conn:
            return [
                self._row_to_directive(r)
                for r in conn.execute(stmt).mappings()
            ]

    def get_by_id(self, directive_id: str) -> Directive | None:
        stmt = sa.select(directives).where(directives.c.id == directive_id)
        with self._db.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_directive(row) if row else None

    def update_status(
        self,
        directive_id: str,
        new_status: DirectiveStatus,
        reason: str | None = None,
    ) -> None:
        """Soft-disable lifecycle write per memory contract.

        Raises:
            ValueError — invalid new_status, or reason rule violation
                (DISABLED requires reason; others must have reason=None).
            LookupError — directive_id not found.
        """
        if not isinstance(new_status, DirectiveStatus):
            raise ValueError(
                f"new_status must be DirectiveStatus, got {new_status!r}"
            )

        now = datetime.now(timezone.utc)

        if new_status is DirectiveStatus.DISABLED:
            if reason is None:
                raise ValueError(
                    "update_status: DISABLED transitions require a "
                    "non-None reason (audit-trail contract — see memory: "
                    "directive lifecycle)."
                )
            values = {
                "status": new_status.value,
                "disabled_at": now,
                "disabled_reason": reason,
                "updated_at": now,
            }
        else:
            if reason is not None:
                raise ValueError(
                    f"update_status: non-DISABLED transition to "
                    f"{new_status.value!r} must NOT carry a reason "
                    f"(got {reason!r})"
                )
            values = {
                "status": new_status.value,
                "disabled_at": None,
                "disabled_reason": None,
                "updated_at": now,
            }

        stmt = (
            sa.update(directives)
            .where(directives.c.id == directive_id)
            .values(**values)
        )
        with self._db.engine.begin() as conn:
            result = conn.execute(stmt)
            if result.rowcount == 0:
                raise LookupError(
                    f"directive {directive_id!r} not found; "
                    "update_status will not silently no-op"
                )

    @staticmethod
    def _guard(directive: Directive) -> None:
        """Defensive re-validation against model_construct() bypass.

        Validates that:
        1. `status` is a real DirectiveStatus value.
        2. `type` is a real DirectiveType value.
        3. The lifecycle invariant holds: disabled_at and disabled_reason
           are non-None iff status is DISABLED. This invariant is
           normally upheld by `update_status`, but `insert` is a
           separate code path; without this check, a model_construct
           bypass with status='active' + disabled_at=<datetime> would
           land an incoherent row.
        """
        # 1. status enum
        if not isinstance(directive.status, DirectiveStatus):
            try:
                DirectiveStatus(directive.status)
            except ValueError as e:
                raise ValueError(
                    f"Directive.status={directive.status!r} not in "
                    f"DirectiveStatus enum"
                ) from e

        # 2. type enum
        if not isinstance(directive.type, DirectiveType):
            try:
                DirectiveType(directive.type)
            except ValueError as e:
                raise ValueError(
                    f"Directive.type={directive.type!r} not in "
                    f"DirectiveType enum"
                ) from e

        # 3. lifecycle coherence: disabled_* iff status==DISABLED
        is_disabled = directive.status is DirectiveStatus.DISABLED
        has_disabled_at = directive.disabled_at is not None
        has_disabled_reason = directive.disabled_reason is not None
        if is_disabled and not (has_disabled_at and has_disabled_reason):
            raise ValueError(
                "Directive lifecycle invariant violated: status=DISABLED "
                "requires both disabled_at and disabled_reason set "
                f"(got disabled_at={directive.disabled_at!r}, "
                f"disabled_reason={directive.disabled_reason!r})"
            )
        if not is_disabled and (has_disabled_at or has_disabled_reason):
            raise ValueError(
                f"Directive lifecycle invariant violated: status="
                f"{directive.status.value!r} cannot carry disabled_at or "
                f"disabled_reason (got disabled_at={directive.disabled_at!r}, "
                f"disabled_reason={directive.disabled_reason!r})"
            )

    @staticmethod
    def _enum_value(value: object, enum_cls: type) -> str:
        """Coerce enum or raw string into the canonical enum value."""
        if isinstance(value, enum_cls):
            return value.value  # type: ignore[attr-defined]
        return enum_cls(value).value  # type: ignore[no-any-return]

    @classmethod
    def _directive_to_row(cls, d: Directive) -> dict[str, Any]:
        return {
            "id": d.id,
            "project_id": d.project_id,
            "type": cls._enum_value(d.type, DirectiveType),
            "status": cls._enum_value(d.status, DirectiveStatus),
            "instruction": d.instruction,
            "frequency": d.frequency,
            "trigger_pattern": d.trigger_pattern,
            "confidence": d.confidence,
            "max_firing": d.max_firing,
            "source_flag_type": d.source_flag_type,
            "source_sessions": json.dumps(
                d.source_sessions, ensure_ascii=False
            ),
            "created_at": d.created_at,
            "expires_at": d.expires_at,
            "updated_at": d.updated_at,
            "disabled_at": d.disabled_at,
            "disabled_reason": d.disabled_reason,
        }

    @staticmethod
    def _row_to_directive(row: sa.RowMapping) -> Directive:
        return Directive(
            id=row["id"],
            project_id=row["project_id"],
            type=DirectiveType(row["type"]),
            status=DirectiveStatus(row["status"]),
            instruction=row["instruction"],
            frequency=row["frequency"],
            trigger_pattern=row["trigger_pattern"],
            confidence=row["confidence"],
            max_firing=row["max_firing"],
            source_flag_type=row["source_flag_type"],
            source_sessions=json.loads(row["source_sessions"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            updated_at=row["updated_at"],
            disabled_at=row["disabled_at"],
            disabled_reason=row["disabled_reason"],
        )
