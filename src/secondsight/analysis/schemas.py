"""Analysis-layer Pydantic contracts and enums (GUR-100 task-1).

Single source of truth for the BehaviorFlagType vocabulary (SD §5.5.1)
and the data shapes the analysis prompt builder (GUR-101) and the Phase-2
repositories consume.

Death cases enforced here (Pydantic v2 validation):
- BehaviorFlag.flag_type rejects values outside BehaviorFlagType.
- BehaviorFlag.confidence rejects values outside {high, medium, low}.
- Directive.status rejects values outside DirectiveStatus.
- ToolUseSpan rejects (success=True, duration_ms=None) — a successful
  span MUST carry a measured duration; orphan starts use success=None.

Repositories MUST re-validate via the same enums on insert because
`model_construct()` bypasses Pydantic. That defensive guard lives in
behavior_flags_repository.py and directives_repository.py (D1).

SD references:
- §5.5.1 — BehaviorFlagType vocabulary (six values).
- §5.5.2 — segment-level analysis prompt; `confidence` field is added
  to the output schema in the same PR (D3 ship gate).
- §7.4   — directives DDL; `disabled_at` / `disabled_reason` columns
  are added in the same PR (D3 ship gate).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator


# ---------- Enums (string-valued for direct DB persistence) ----------


class BehaviorFlagType(str, Enum):
    """Per SD §5.5.1 — single source of truth for the analysis prompt.

    Six values. Adding a value requires a co-modified SD §5.5.1 update
    (and downstream prompt rebuild on GUR-101).
    """

    UNNECESSARY_READ = "unnecessary_read"
    REDUNDANT_EXPLORATION = "redundant_exploration"
    MISSED_SHORTCUT = "missed_shortcut"
    REPEATED_OPERATION = "repeated_operation"
    WRONG_TOOL_CHOICE = "wrong_tool_choice"
    EXCESSIVE_CONTEXT_GATHERING = "excessive_context_gathering"


class DirectiveStatus(str, Enum):
    """Per SD §7.4. Five values; user-PATCH surface (GUR-104) accepts
    only {active, disabled} — the other three are analyzer-set.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    OBSOLETE = "obsolete"


class DirectiveType(str, Enum):
    """Per SD §7.4. `hint` is reserved for future use; v1 ships only
    `convention` directives.
    """

    CONVENTION = "convention"
    HINT = "hint"


# ---------- Pydantic models ----------


class BehaviorFlag(BaseModel):
    """Per SD §5.5.2 (with `confidence` field added — D3 SD patch).

    One row per detected behavior flag. Persisted to the
    `behavior_flags` table (SD §7.3) and consumed by the analysis
    prompt-output parser in GUR-101.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    session_id: str
    segment_index: int
    flag_type: BehaviorFlagType
    event_ids: list[str]
    intent_summary: str
    reason: str
    confidence: Literal["high", "medium", "low"]
    created_at: datetime


class Directive(BaseModel):
    """Per SD §7.4 (with `disabled_at` / `disabled_reason` added — D3).

    Lifecycle status mutates over time via DirectivesRepository.update_status.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    type: DirectiveType
    status: DirectiveStatus
    instruction: str
    frequency: float | None = None
    trigger_pattern: str | None = None  # hint reserved
    confidence: float | None = None  # hint reserved
    max_firing: int | None = None  # hint reserved
    source_flag_type: str | None = None
    source_sessions: list[str] = []
    created_at: datetime
    expires_at: datetime | None = None
    updated_at: datetime
    disabled_at: datetime | None = None
    disabled_reason: str | None = None


class ToolUseSpan(BaseModel):
    """Paired tool_use_start/end as a single logical span.

    Death cases:
    - `success=True` with `duration_ms=None` is incoherent and rejected
      by the model_validator below. A successful span carries a measured
      duration; orphan starts use `success=None`.
    - Orphan tool_use_start: `success=None`, `duration_ms=None`,
      `end_seq=None`. The span is emitted (never silently dropped).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    target: str | None
    success: bool | None  # None = unknown (orphan start)
    duration_ms: int | None
    start_seq: int
    end_seq: int | None  # None = orphan start (no matching end)
    metadata: dict[str, Any] = {}

    @model_validator(mode="after")
    def _success_requires_duration(self) -> "ToolUseSpan":
        if self.success is True and self.duration_ms is None:
            raise ValueError(
                "ToolUseSpan: success=True with duration_ms=None is "
                "incoherent. Successful spans MUST carry a measured "
                "duration; orphan starts must use success=None."
            )
        return self


class SegmentData(BaseModel):
    """The shape the LLM analysis prompt (SD §5.5.2) consumes.

    `user_prompt=None` for the implicit segment_index=0 pre-prompt
    segment (events that arrived before any USER_PROMPT). `events` is
    intentionally heterogeneous: ToolUseSpan for paired tool-uses,
    raw dict for thinking/response/sub_agent events.

    Trade-off acknowledged: callers must dispatch on type. A tagged
    union would be more verbose without solving a real problem at v1.
    """

    model_config = ConfigDict(extra="forbid")

    segment_index: int
    user_prompt: dict[str, Any] | None
    events: list[ToolUseSpan | dict[str, Any]]
    session_id: str
    project_id: str


class SegmentMetrics(TypedDict):
    """Per SD §5.3.1 step 2. Pure-function output of compute_segment_metrics."""

    total_tokens: int
    unique_files: int
    duration: float  # seconds
    error_count: int
