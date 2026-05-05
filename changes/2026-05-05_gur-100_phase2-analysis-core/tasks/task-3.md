# Task 3: directives table + repository (with lifecycle)

## Context

Read: `overview.md` for full architecture. Task-1 must be complete:
this task imports `Directive`, `DirectiveStatus`, `DirectiveType` from
`secondsight.analysis.schemas`.

This task ships SD §7.4's `directives` table plus a repository that
mirrors `EventsRepository`'s idempotency contract. **Memory contract**
(`project_directive_lifecycle_contract.md`): the table includes
`disabled_at` and `disabled_reason` columns NOT yet in SD §7.4 — those
columns ship here, and the SD patch lands in task-5 per **D3**.

The lifecycle method `update_status(directive_id, new_status,
reason=None)` is the in-process surface that GUR-104's HTTP PATCH
endpoint will wrap. The semantics are the **soft-disable contract**:

- `→ disabled` REQUIRES non-None `reason`; sets `disabled_at = now()`,
  writes `disabled_reason`.
- `→ active` from any status: clears `disabled_at = NULL`,
  `disabled_reason = NULL`. `reason` MUST be None for non-disabled
  transitions (passing one is a contract violation).
- `→ superseded`, `→ expired`, `→ obsolete`: analyzer-set; same
  null-clearing rule as `→ active`.

## Files

- Create: `src/secondsight/storage/directives_table.py`
- Create: `src/secondsight/storage/directives_repository.py`
- Create: `tests/storage/test_directives_repository.py`
- Modify: `src/secondsight/storage/__init__.py` (re-export new repo)

## Death Test Requirements

Write these BEFORE implementation.

- **DT-3.1** — `update_status(id, DirectiveStatus.DISABLED, reason=None)`
  raises `ValueError` naming the missing reason. No UPDATE issued
  (verify by re-reading the row; status unchanged).
- **DT-3.2** — `update_status(id, DirectiveStatus.ACTIVE, reason="late")`
  raises `ValueError` naming "non-disabled transitions cannot carry
  a reason".
- **DT-3.3** — `Directive.model_construct(status="frozen", ...)` →
  `repo.insert(directive)` raises `ValueError`. No row written.
- **DT-3.4** — Round trip: insert active → disable with reason
  "test-1" → re-active → re-read. Row shows `status='active'`,
  `disabled_at IS NULL`, `disabled_reason IS NULL`. Without the clear,
  re-active rows still show stale "test-1" reason.
- **DT-3.5** — `update_status(id, DirectiveStatus.SUPERSEDED)` (no
  reason) succeeds; `disabled_at` and `disabled_reason` remain
  whatever they were (this transition does NOT clear them — only
  `→ active` does). **Wait**: rethink — do superseded/expired clear
  the disable metadata? Decision: yes, because they are valid
  "out of disabled" transitions. Specify in code: any non-disabled
  status clears both null columns.

## Implementation Steps

- [ ] Step 1: Read `events_repository.py` and the task-2
      `behavior_flags_repository.py` to mirror style.
- [ ] Step 2: Write `tests/storage/test_directives_repository.py`
      with DT-3.1..DT-3.5 plus 4 happy-path tests:
      - insert + get_active_conventions returns sorted by frequency DESC
      - get_by_id round-trip
      - update_status active→disabled→active full cycle observable
      - get_active_conventions filters out disabled directives
- [ ] Step 3: Run tests — all red.
- [ ] Step 4: Write `directives_table.py` per SD §7.4 + memory contract
      additions. **The SD §7.4 patch itself lands in task-5.**
- [ ] Step 5: Write `directives_repository.py` per the spec below.
- [ ] Step 6: Run tests — all green.
- [ ] Step 7: Run full test suite — no regressions.
- [ ] Step 8: Write scar report. Commit.

## Spec — `src/secondsight/storage/directives_table.py`

```python
"""directives table — SQLAlchemy Core schema (SD §7.4 + memory contract).

The `disabled_at` and `disabled_reason` columns are additions to SD §7.4
mandated by `project_directive_lifecycle_contract.md`. The SD patch
that adds them to the canonical DDL is part of task-5 (D3 ship gate).

`status` column is TEXT; validation lives at the repository layer
(D1 — mirrors events.event_type convention).
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

directives = sa.Table(
    "directives",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),  # convention | hint
    sa.Column("status", sa.Text, nullable=False),  # active|disabled|expired|superseded|obsolete
    sa.Column("instruction", sa.Text, nullable=False),
    sa.Column("frequency", sa.Float, nullable=True),
    sa.Column("trigger_pattern", sa.Text, nullable=True),  # hint reserved
    sa.Column("confidence", sa.Float, nullable=True),  # hint reserved
    sa.Column("max_firing", sa.Integer, nullable=True),  # hint reserved
    sa.Column("source_flag_type", sa.Text, nullable=True),
    sa.Column("source_sessions", sa.Text, nullable=False, server_default="[]"),  # JSON-encoded
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("expires_at", sa.DateTime, nullable=True),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("disabled_at", sa.DateTime, nullable=True),
    sa.Column("disabled_reason", sa.Text, nullable=True),
)

sa.Index("idx_directives_project_status", directives.c.project_id, directives.c.status)
sa.Index("idx_directives_project_type", directives.c.project_id, directives.c.type)
```

## Spec — `src/secondsight/storage/directives_repository.py`

```python
"""DirectivesRepository — SQLAlchemy Core repository (GUR-100 task-3).

Lifecycle contract (per project_directive_lifecycle_contract memory):
- `update_status(id, DISABLED, reason=...)` requires non-None reason.
  Sets disabled_at=now(), disabled_reason=reason.
- `update_status(id, <not-DISABLED>)` requires reason=None.
  Clears disabled_at=NULL, disabled_reason=NULL.
- updated_at advances on every transition.
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
            return [self._row_to_directive(r) for r in conn.execute(stmt).mappings()]

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
        if not isinstance(new_status, DirectiveStatus):
            raise ValueError(
                f"new_status must be DirectiveStatus, got {new_status!r}"
            )

        if new_status is DirectiveStatus.DISABLED:
            if reason is None:
                raise ValueError(
                    "update_status: DISABLED transitions require a reason "
                    "(audit-trail contract — see memory: directive lifecycle)"
                )
            now = datetime.now(timezone.utc)
            values = {
                "status": new_status.value,
                "disabled_at": now,
                "disabled_reason": reason,
                "updated_at": now,
            }
        else:
            if reason is not None:
                raise ValueError(
                    f"update_status: non-DISABLED transition to {new_status.value!r} "
                    "must NOT carry a reason"
                )
            now = datetime.now(timezone.utc)
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
                raise LookupError(f"directive {directive_id!r} not found")

    @staticmethod
    def _guard(directive: Directive) -> None:
        """Defensive re-validation against model_construct() bypass."""
        if not isinstance(directive.status, DirectiveStatus):
            raise ValueError(
                f"Directive.status must be DirectiveStatus, "
                f"got {directive.status!r}"
            )
        if not isinstance(directive.type, DirectiveType):
            raise ValueError(
                f"Directive.type must be DirectiveType, got {directive.type!r}"
            )
        # Sanity: re-construct enums to surface bypass values.
        DirectiveStatus(directive.status.value)
        DirectiveType(directive.type.value)

    @staticmethod
    def _directive_to_row(d: Directive) -> dict[str, Any]:
        return {
            "id": d.id,
            "project_id": d.project_id,
            "type": d.type.value,
            "status": d.status.value,
            "instruction": d.instruction,
            "frequency": d.frequency,
            "trigger_pattern": d.trigger_pattern,
            "confidence": d.confidence,
            "max_firing": d.max_firing,
            "source_flag_type": d.source_flag_type,
            "source_sessions": json.dumps(d.source_sessions, ensure_ascii=False),
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
```

## Expected Scar Report Items

- Potential shortcut: skip the `LookupError` on `update_status` when
  the directive doesn't exist, return silently — rejected; silent
  no-op on a typo'd id is exactly the silent-failure pattern the
  contract closes.
- Potential shortcut: allow `→ disabled` without a reason "if reason
  is the empty string" — rejected; an empty-string reason fails the
  audit-trail intent. Either reject or treat empty as missing.
- Potential shortcut: don't re-clear `disabled_at` / `disabled_reason`
  on `→ superseded` because "superseded ≠ active" — rejected; only
  the disabled state owns those fields. Any non-disabled transition
  clears them.
- Assumption to verify: `directives` table reuses the same `metadata`
  as `events_table` and `behavior_flags_table`, so a single
  `metadata.create_all()` call brings up all three.

## Acceptance Criteria

Covers the following acceptance.yaml scenarios:
- "Silent failure - directive status drift via model_construct bypass"
- "Silent failure - disabled transition without reason"
- "Silent failure - re-active leaves stale disabled metadata"
- "Degradation - directive expires_at column carries unverified time"
  (this task ships the column; no enforcement — comment in code)
- "Success - directive lifecycle active → disabled → active"
