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

GUR-102 identity_key constraint (task-1):
    The directives table gained UNIQUE(project_id, identity_key) in GUR-102.
    The server_default="" is a transitional DDL value — valid only because
    the table is empty pre-Phase 3. insert() is for non-aggregator code paths
    that don't assign identity_key; callers MUST supply a non-empty, unique
    identity_key per (project_id) to avoid IntegrityError. Use
    upsert_with_identity_key() for the Phase 2 aggregator path.

    If your code creates multiple directives for the same project without
    setting identity_key, use distinct non-empty values (e.g., the directive
    id) as a surrogate until the aggregator assigns canonical sha256 keys.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_table import directives, metadata

_logger = logging.getLogger(__name__)


class DirectivesRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        """Create directives + indexes if absent. Idempotent."""
        metadata.create_all(self._db.engine, checkfirst=True)

    def insert(self, directive: Directive) -> None:
        """Insert a directive. Idempotent on `id` (ON CONFLICT DO NOTHING).

        WARNING (GUR-102): If directive.identity_key is empty (""), a second
        insert() in the same project with identity_key="" will raise
        ValueError (from the UNIQUE(project_id, identity_key) constraint via
        uq_directives_project_identity). Callers must supply a non-empty,
        unique identity_key per project.
        Use upsert_with_identity_key() for Phase 2 aggregator paths.

        Raises:
            ValueError — UNIQUE(project_id, identity_key) constraint violated
                (constraint name: uq_directives_project_identity). This happens
                when two directives with identity_key="" are inserted for the
                same project, or when any two directives share the same
                non-empty identity_key within a project.
        """
        if not directive.identity_key:
            _logger.warning(
                "DirectivesRepository.insert: directive.id=%r has empty "
                "identity_key. A second directive in project_id=%r with "
                "identity_key='' will raise ValueError. Assign a unique "
                "non-empty identity_key or use upsert_with_identity_key().",
                directive.id,
                directive.project_id,
            )
        self._guard(directive)
        row = self._directive_to_row(directive)
        stmt = sqlite_insert(directives).values(**row).on_conflict_do_nothing(index_elements=["id"])
        try:
            with self._db.engine.begin() as conn:
                conn.execute(stmt)
        except IntegrityError as exc:
            raise ValueError(
                f"DirectivesRepository.insert: UNIQUE constraint violated "
                f"(uq_directives_project_identity) for directive.id={directive.id!r}, "
                f"project_id={directive.project_id!r}, "
                f"identity_key={directive.identity_key!r}. "
                f"Two directives in the same project cannot share identity_key. "
                f'If identity_key is empty (""), use distinct non-empty keys '
                f"or switch to upsert_with_identity_key()."
            ) from exc

    _MAX_CONVENTIONS = 100

    def get_active_conventions(self, project_id: str) -> list[Directive]:
        """Active, non-expired conventions sorted by frequency desc.

        Filters out rows whose expires_at is in the past (defense-in-depth;
        the analyzer should have transitioned these to 'expired' status, but
        a race or crash could leave stale rows). Limited to _MAX_CONVENTIONS
        to bound memory and query cost regardless of DB contents.
        """
        now = datetime.now(tz=timezone.utc)
        stmt = (
            sa.select(directives)
            .where(
                sa.and_(
                    directives.c.project_id == project_id,
                    directives.c.type == DirectiveType.CONVENTION.value,
                    directives.c.status == DirectiveStatus.ACTIVE.value,
                    sa.or_(
                        directives.c.expires_at.is_(None),
                        directives.c.expires_at > now,
                    ),
                )
            )
            .order_by(directives.c.frequency.desc().nullslast())
            .limit(self._MAX_CONVENTIONS)
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_directive(r) for r in conn.execute(stmt).mappings()]

    def get_by_id(self, directive_id: str) -> Directive | None:
        stmt = sa.select(directives).where(directives.c.id == directive_id)
        with self._db.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_directive(row) if row else None

    def list_for_project(self, project_id: str, *, active_only: bool = False) -> list[Directive]:
        """List directives for a project ordered by ``updated_at`` DESC.

        Backs ``GET /api/directives?active=<bool>`` (GUR-104). When
        ``active_only=True``, equivalent in scope to
        ``get_active_conventions`` but ordered by recency rather than
        frequency — kept distinct because the listing UI cares about
        recency, while the agent self-query path cares about frequency.

        Sort order: ``updated_at DESC, id ASC`` — deterministic
        tie-break for rows whose ``updated_at`` collide on the same
        microsecond.
        """
        _API_VISIBLE = [DirectiveStatus.ACTIVE.value, DirectiveStatus.DISABLED.value]
        where = [directives.c.project_id == project_id]
        if active_only:
            where.append(directives.c.status == DirectiveStatus.ACTIVE.value)
        else:
            where.append(directives.c.status.in_(_API_VISIBLE))
        stmt = (
            sa.select(directives)
            .where(*where)
            .order_by(directives.c.updated_at.desc(), directives.c.id.asc())
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_directive(r) for r in conn.execute(stmt).mappings()]

    def upsert_with_identity_key(self, directive: Directive) -> None:
        """ON CONFLICT(project_id, identity_key) DO UPDATE SET
           instruction, frequency, source_sessions, updated_at.
           status, type, source_flag_type, created_at preserved on UPDATE.

        Used by the Phase 2 aggregator to create or update directives
        keyed by content identity (flag_type + session set) rather than
        by opaque UUID.

        NOTE — id discard on conflict: On (project_id, identity_key) conflict,
        the existing row's `id` is preserved; the incoming `directive.id` is
        discarded. The `id` column is NOT in the UPDATE clause. The aggregate
        caller should use get_by_id or query by (project_id, identity_key) to
        retrieve the canonical id after upsert if needed.

        Raises:
            ValueError — identity_key is empty (server_default="" is a
                transitional DDL default only; no real row should carry it).
            ValueError — defensive guard rejects invalid status/type.
        """
        if not directive.identity_key:
            raise ValueError(
                "upsert_with_identity_key: identity_key must not be empty. "
                "The server_default='' is a transitional DDL default only; "
                "the repository rejects it to prevent UNIQUE(project_id, "
                "identity_key) collisions on empty-key rows."
            )
        self._guard(directive)
        row = self._directive_to_row(directive)
        stmt = (
            sqlite_insert(directives)
            .values(**row)
            .on_conflict_do_update(
                index_elements=[
                    directives.c.project_id,
                    directives.c.identity_key,
                ],
                set_={
                    "instruction": row["instruction"],
                    "frequency": row["frequency"],
                    "source_sessions": row["source_sessions"],
                    "updated_at": row["updated_at"],
                    # Preserved (NOT updated): status, type, source_flag_type,
                    # created_at, disabled_at, disabled_reason.
                },
            )
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

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
            raise ValueError(f"new_status must be DirectiveStatus, got {new_status!r}")

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

        stmt = sa.update(directives).where(directives.c.id == directive_id).values(**values)
        with self._db.engine.begin() as conn:
            result = conn.execute(stmt)
            if result.rowcount == 0:
                raise LookupError(
                    f"directive {directive_id!r} not found; update_status will not silently no-op"
                )

    def compare_and_update_status(
        self,
        directive_id: str,
        project_id: str,
        new_status: DirectiveStatus,
        reason: str | None = None,
    ) -> tuple[Directive | None, bool]:
        """Atomic read-compare-write for PATCH idempotency (DC-2, H-1).

        Returns (directive, was_noop):
        - (None, False) if directive not found or cross-project mismatch
        - (directive, True) if current state already matches requested state
        - (directive, False) if update was applied

        The entire operation runs in a single BEGIN transaction so
        concurrent PATCHes serialize at the SQLite write-lock level.
        """
        if not isinstance(new_status, DirectiveStatus):
            raise ValueError(f"new_status must be DirectiveStatus, got {new_status!r}")

        now = datetime.now(timezone.utc)

        with self._db.engine.begin() as conn:
            row = (
                conn.execute(sa.select(directives).where(directives.c.id == directive_id))
                .mappings()
                .first()
            )

            if row is None:
                return None, False

            current = self._row_to_directive(row)
            if current.project_id != project_id:
                return None, False

            is_noop = current.status == new_status and (
                new_status != DirectiveStatus.DISABLED or current.disabled_reason == reason
            )
            if is_noop:
                return current, True

            if new_status is DirectiveStatus.DISABLED:
                if reason is None:
                    raise ValueError("DISABLED transitions require a non-None reason.")
                values = {
                    "status": new_status.value,
                    "disabled_at": now,
                    "disabled_reason": reason,
                    "updated_at": now,
                }
            else:
                if reason is not None:
                    raise ValueError(
                        f"Non-DISABLED transition to {new_status.value!r} "
                        f"must NOT carry a reason (got {reason!r})"
                    )
                values = {
                    "status": new_status.value,
                    "disabled_at": None,
                    "disabled_reason": None,
                    "updated_at": now,
                }

            conn.execute(
                sa.update(directives).where(directives.c.id == directive_id).values(**values)
            )

            refreshed_row = (
                conn.execute(sa.select(directives).where(directives.c.id == directive_id))
                .mappings()
                .first()
            )
            if refreshed_row is None:
                raise LookupError(f"directive {directive_id!r} disappeared after update")
            return self._row_to_directive(refreshed_row), False

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
                    f"Directive.status={directive.status!r} not in DirectiveStatus enum"
                ) from e

        # 2. type enum
        if not isinstance(directive.type, DirectiveType):
            try:
                DirectiveType(directive.type)
            except ValueError as e:
                raise ValueError(
                    f"Directive.type={directive.type!r} not in DirectiveType enum"
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
    def _enum_value(value: object, enum_cls: type[DirectiveType] | type[DirectiveStatus]) -> str:
        """Coerce enum or raw string into the canonical enum value."""
        if isinstance(value, enum_cls):
            return value.value
        return enum_cls(value).value

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
            "source_sessions": json.dumps(d.source_sessions, ensure_ascii=False),
            "identity_key": d.identity_key,
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
            identity_key=row["identity_key"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            updated_at=row["updated_at"],
            disabled_at=row["disabled_at"],
            disabled_reason=row["disabled_reason"],
        )
