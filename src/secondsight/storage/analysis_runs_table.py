"""analysis_runs table — SQLAlchemy Core schema (GUR-102 task-1).

Audit table for Phase 2 analysis pipeline runs. One row per pipeline
execution, inserted at stage='pending' BEFORE any pipeline work begins
(DC-1: audit trail survives SIGKILL after the insert).

Notes:
- `stage` is TEXT; validation lives at the repository layer (D1 —
  mirrors GUR-100's behavior_flags.flag_type convention). No DB CHECK.
- `flags_inserted` defaults to 0 via server_default; only meaningful
  on the 'behavior_done' stage transition.
- Reuses events_table.metadata so a single metadata.create_all() call
  brings up all tables together.
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

analysis_runs = sa.Table(
    "analysis_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column(
        "stage",
        sa.Text,
        nullable=False,
    ),  # 'pending'|'segmented'|'behavior_done'|'summary_written'|'aggregated'|'failed'
    sa.Column("started_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("completed_at", sa.DateTime, nullable=True),
    sa.Column("error_message", sa.Text, nullable=True),
    sa.Column(
        "flags_inserted",
        sa.Integer,
        nullable=False,
        server_default="0",
    ),
)

sa.Index(
    "idx_ar_project_session",
    analysis_runs.c.project_id,
    analysis_runs.c.session_id,
)
sa.Index(
    "idx_ar_project_stage",
    analysis_runs.c.project_id,
    analysis_runs.c.stage,
)
