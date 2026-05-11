# Task 2: behavior_flags table + repository

## Context

Read: `overview.md` for full architecture. Task-1 must be complete:
this task imports `BehaviorFlag`, `BehaviorFlagType` from
`secondsight.analysis.schemas`.

This task ships SD §7.3's `behavior_flags` table plus a repository
that mirrors the existing `EventsRepository` idempotency contract:
`INSERT … ON CONFLICT(id) DO NOTHING`. Per **D1**, the repository
adds a defensive enum guard at `insert()` to close the
`model_construct()` bypass.

The reference implementation to mirror is
`src/secondsight/storage/events_repository.py`. Read it first.

## Files

- Create: `src/secondsight/storage/behavior_flags_table.py`
- Create: `src/secondsight/storage/behavior_flags_repository.py`
- Create: `tests/storage/test_behavior_flags_repository.py`
- Modify: `src/secondsight/storage/__init__.py` (re-export the new
  repo class for ergonomic imports)

## Death Test Requirements

Write these BEFORE implementation. Each must be red against an empty
repository module.

- **DT-2.1** — A `BehaviorFlag` constructed via
  `BehaviorFlag.model_construct(flag_type="bogus_type", ...)` →
  `repo.insert(flag)` raises `ValueError` naming the bad enum value.
  Without the defensive guard, the row would silently land in the DB
  with `flag_type='bogus_type'`. Verify by querying the DB after the
  expected exception is caught: zero rows must exist.
- **DT-2.2** — Two `insert()` calls with the **same `id`** but
  different `flag_type` → only the FIRST persists (ON CONFLICT
  DO NOTHING). Use `get_session_flags` to verify; the returned flag
  must have the first `flag_type`, not the second. The test fails if
  the table contains 2 rows or if the second `flag_type` won.
- **DT-2.3** — `insert(flag)` followed by querying with a fresh
  repository instance against the same `DBEngine` returns the
  inserted flag. Detects the silent-failure where the INSERT was
  buffered but never committed.

## Implementation Steps

- [ ] Step 1: Read `src/secondsight/storage/events_repository.py` and
      `events_table.py` end-to-end. Match its style precisely.
- [ ] Step 2: Write `tests/storage/test_behavior_flags_repository.py`
      with DT-2.1..DT-2.3 plus 4 happy-path tests:
      - insert + get_session_flags round-trip with `event_ids` JSON
      - insert_many(50) returns 50; count_by_type sums correctly
      - get_project_flags_by_type filters correctly
      - create_schema is idempotent (call twice, no error)
- [ ] Step 3: Run tests — all red.
- [ ] Step 4: Write `behavior_flags_table.py` per SD §7.3 + the new
      `confidence` column.
- [ ] Step 5: Write `behavior_flags_repository.py` per the spec below.
- [ ] Step 6: Run tests — all green.
- [ ] Step 7: Run full test suite — no regressions.
- [ ] Step 8: Write scar report. Commit.

## Spec — `src/secondsight/storage/behavior_flags_table.py`

```python
"""behavior_flags table — SQLAlchemy Core schema (SD §7.3, GUR-100).

Holds Phase 2 LLM-analysis output (one row per detected behavior flag).
The `confidence` column is an addition not in the original SD §7.3 — it
ships here per memory contract and lands in the same PR as the SD §5.5.2
patch (D3).
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata  # share metadata

behavior_flags = sa.Table(
    "behavior_flags",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("segment_index", sa.Integer, nullable=False),
    sa.Column("flag_type", sa.Text, nullable=False),
    sa.Column("event_ids", sa.Text, nullable=False),  # JSON-encoded list[str]
    sa.Column("intent_summary", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("confidence", sa.Text, nullable=False),  # 'high' | 'medium' | 'low'
    sa.Column("created_at", sa.DateTime, nullable=False),
)

sa.Index(
    "idx_bf_project_session",
    behavior_flags.c.project_id,
    behavior_flags.c.session_id,
)
sa.Index(
    "idx_bf_project_type",
    behavior_flags.c.project_id,
    behavior_flags.c.flag_type,
)
```

**Note:** the table reuses `metadata` from `events_table.py` so a
single `metadata.create_all()` call brings up all Phase 1 + Phase 2
tables together.

## Spec — `src/secondsight/storage/behavior_flags_repository.py`

```python
"""BehaviorFlagsRepository — SQLAlchemy Core repository (GUR-100 task-2).

Idempotency contract:
    insert(flag) is idempotent on `id`. Two calls with the same id and
    different fields persist only the FIRST (ON CONFLICT DO NOTHING).

Defensive enum guard:
    BehaviorFlag.model_construct() bypasses Pydantic. The repository
    re-validates flag_type and confidence on insert to close that
    silent-failure surface (D1).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from secondsight.analysis.schemas import BehaviorFlag, BehaviorFlagType
from secondsight.storage.behavior_flags_table import behavior_flags, metadata
from secondsight.storage.db_engine import DBEngine

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


class BehaviorFlagsRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        metadata.create_all(self._db.engine, checkfirst=True)

    def insert(self, flag: BehaviorFlag) -> None:
        self._guard(flag)
        row = self._flag_to_row(flag)
        stmt = (
            sqlite_insert(behavior_flags)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt)

    def insert_many(self, flags: Sequence[BehaviorFlag]) -> int:
        if not flags:
            return 0
        for flag in flags:
            self._guard(flag)
        rows = [self._flag_to_row(f) for f in flags]
        stmt = sqlite_insert(behavior_flags).on_conflict_do_nothing(
            index_elements=["id"]
        )
        with self._db.engine.begin() as conn:
            conn.execute(stmt, rows)
        return len(rows)

    def get_session_flags(self, session_id: str) -> list[BehaviorFlag]:
        stmt = (
            sa.select(behavior_flags)
            .where(behavior_flags.c.session_id == session_id)
            .order_by(behavior_flags.c.created_at.asc())
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_flag(r) for r in conn.execute(stmt).mappings()]

    def get_project_flags_by_type(
        self, project_id: str, flag_type: BehaviorFlagType
    ) -> list[BehaviorFlag]:
        stmt = sa.select(behavior_flags).where(
            sa.and_(
                behavior_flags.c.project_id == project_id,
                behavior_flags.c.flag_type == flag_type.value,
            )
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_flag(r) for r in conn.execute(stmt).mappings()]

    def count_by_type(self, project_id: str) -> dict[BehaviorFlagType, int]:
        stmt = (
            sa.select(behavior_flags.c.flag_type, sa.func.count())
            .where(behavior_flags.c.project_id == project_id)
            .group_by(behavior_flags.c.flag_type)
        )
        with self._db.engine.connect() as conn:
            counts: dict[BehaviorFlagType, int] = {}
            for row in conn.execute(stmt):
                # Defensive: a manually-edited DB could carry a flag_type
                # outside the enum. Skip with a WARNING rather than crash.
                try:
                    counts[BehaviorFlagType(row[0])] = int(row[1])
                except ValueError:
                    import logging

                    logging.getLogger(__name__).warning(
                        "behavior_flags.flag_type=%r outside enum; "
                        "skipping in count_by_type",
                        row[0],
                    )
            return counts

    @staticmethod
    def _guard(flag: BehaviorFlag) -> None:
        """Defensive re-validation against model_construct() bypass."""
        if not isinstance(flag.flag_type, BehaviorFlagType):
            raise ValueError(
                f"BehaviorFlag.flag_type must be a BehaviorFlagType, "
                f"got {flag.flag_type!r}"
            )
        try:
            BehaviorFlagType(flag.flag_type.value)
        except ValueError as e:
            raise ValueError(
                f"BehaviorFlag.flag_type={flag.flag_type!r} not in enum"
            ) from e
        if flag.confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"BehaviorFlag.confidence={flag.confidence!r} must be "
                f"one of {_VALID_CONFIDENCE}"
            )

    @staticmethod
    def _flag_to_row(flag: BehaviorFlag) -> dict[str, Any]:
        return {
            "id": flag.id,
            "project_id": flag.project_id,
            "session_id": flag.session_id,
            "segment_index": flag.segment_index,
            "flag_type": flag.flag_type.value,
            "event_ids": json.dumps(flag.event_ids, ensure_ascii=False),
            "intent_summary": flag.intent_summary,
            "reason": flag.reason,
            "confidence": flag.confidence,
            "created_at": flag.created_at,
        }

    @staticmethod
    def _row_to_flag(row: sa.RowMapping) -> BehaviorFlag:
        return BehaviorFlag(
            id=row["id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            segment_index=row["segment_index"],
            flag_type=BehaviorFlagType(row["flag_type"]),
            event_ids=json.loads(row["event_ids"]),
            intent_summary=row["intent_summary"],
            reason=row["reason"],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )
```

## Expected Scar Report Items

- Potential shortcut: skip the defensive `_guard` on `insert_many`
  loop because "guard is already called per-element above" — confirm
  guard runs for every element in the batch.
- Potential shortcut: trust `model_construct()` callers to validate
  themselves — rejected; D1 puts the guard at the repository.
- Potential shortcut: import `metadata` from a new private module
  instead of reusing `events_table.metadata` — rejected; one
  `metadata` keeps `create_all()` single-call coverage.
- Assumption to verify: `confidence` column is TEXT, not INTEGER.
  Both schema and repository must agree; mis-typing in the table
  would silently coerce values.

## Acceptance Criteria

Covers the following acceptance.yaml scenarios:
- "Silent failure - flag_type drift via Pydantic bypass"
- "Success - BehaviorFlag round-trips through repository"
