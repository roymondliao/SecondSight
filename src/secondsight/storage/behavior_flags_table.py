"""behavior_flags table — SQLAlchemy Core schema (SD §7.3, GUR-100 task-2).

Holds Phase 2 LLM-analysis output (one row per detected behavior flag).

Notes:
- The `confidence` column is NOT in the original SD §7.3 — it ships
  here per memory contract `project_behaviorflag_schema_contract` and
  lands in the same PR as the SD §5.5.2 patch (D3 ship gate).
- Reuses the events_table.metadata so a single
  `metadata.create_all(engine, checkfirst=True)` call brings up Phase 1
  + Phase 2 tables together.
- No DB CHECK constraint on `flag_type` or `confidence` — validation
  lives at the Pydantic + repository layer (D1, mirrors
  events.event_type convention).
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

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
