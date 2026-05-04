"""Production Event model — aligned with SD §3.7.5.

This is the on-the-wire and on-disk shape of an observed event after the
adapter layer has normalized agent-specific payloads. It is deliberately
narrower than the PoC `SecondSightEvent` (poc/event_schema.py): adapter
specifics live in Phase 1.3 normalizers; this model is the storage contract.

Why Pydantic v2:
- Project already depends on pydantic>=2.13 (pyproject.toml).
- We need JSON round-trip with strict types for the on-disk JSON format,
  validation at the API boundary, and frozen instances for safety.

Schema-version field is intentionally omitted from the SQL columns
(SD §3.7.5) and stored only as a top-level field in the on-disk JSON;
column-shape changes are handled via Phase-2 ALTER TABLE migrations.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EVENT_SCHEMA_VERSION = "1.0.0"


class EventType(str, Enum):
    """SD §3.7.2 event types. Adapter layer maps agent hooks to these."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT = "user_prompt"
    THINKING = "thinking"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_END = "tool_use_end"
    SUB_AGENT_START = "sub_agent_start"
    SUB_AGENT_END = "sub_agent_end"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    RESPONSE = "response"


class Event(BaseModel):
    """A single observed event. SD §3.7.5 column shape, plus type-specific
    `data` JSON.

    Frozen (immutable) — events are written once and never mutated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    event_type: EventType
    timestamp: datetime
    sequence_number: int = Field(ge=0)
    segment_index: int = Field(ge=0)

    sub_agent_id: str | None = Field(default=None, max_length=128)
    depth: int = Field(default=0, ge=0)

    duration_ms: int | None = Field(default=None, ge=0)
    token_count: int | None = Field(default=None, ge=0)

    data: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = EVENT_SCHEMA_VERSION
