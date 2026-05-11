"""Events table — SQLAlchemy Core schema (SD §3.7.5).

Single table holding every event type. Type-specific fields go in `data`
JSON. Schema is intentionally identical to the SQL DDL in the design doc;
ALTER TABLE migrations are a Phase 2 concern.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("timestamp", sa.DateTime, nullable=False),
    sa.Column("sequence_number", sa.Integer, nullable=False),
    sa.Column("segment_index", sa.Integer, nullable=False),
    sa.Column("sub_agent_id", sa.Text, nullable=True),
    sa.Column("depth", sa.Integer, nullable=False, server_default="0"),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    sa.Column("token_count", sa.Integer, nullable=True),
    sa.Column("data", sa.Text, nullable=False),
    sa.UniqueConstraint("session_id", "sequence_number", name="uq_events_session_seq"),
)

sa.Index("idx_events_session_seq", events.c.session_id, events.c.sequence_number)
sa.Index("idx_events_segment", events.c.session_id, events.c.segment_index)
sa.Index("idx_events_type", events.c.session_id, events.c.event_type)
sa.Index("idx_events_sub_agent", events.c.session_id, events.c.sub_agent_id)
