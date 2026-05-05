# Task 1: Scaffold — analysis/ package + schemas.py contract anchor

## Context

Read: `overview.md` for full architecture.

GUR-100 ships the analysis-layer data foundation. This task creates
the `src/secondsight/analysis/` package and the contract anchor file
`schemas.py` containing every Pydantic model and enum that tasks 2–5
import. Without this scaffold none of the downstream tasks can start.

The codebase already has `src/secondsight/api/schemas.py` (HTTP request
shapes) — that's a different file. The new `analysis/schemas.py` is
analysis-domain types: `BehaviorFlagType`, `BehaviorFlag`,
`DirectiveStatus`, `DirectiveType`, `Directive`, `ToolUseSpan`,
`SegmentData`, `SegmentMetrics`. **D1** (locked in pre-thinking):
all enums validate at Pydantic construction; repositories add a second
defensive guard at `insert()`. No DB CHECK constraints.

SD references the agent must mirror exactly:
- §5.5.1 — `BehaviorFlagType` six values (verbatim strings).
- §7.4 — `DirectiveStatus` five values (`active`, `disabled`, `expired`,
  `superseded`, `obsolete`); `DirectiveType` two values (`convention`,
  `hint`).

## Files

- Create: `src/secondsight/analysis/__init__.py` (package marker;
  re-exports for ergonomic imports)
- Create: `src/secondsight/analysis/schemas.py` (all enums + Pydantic
  models defined below)
- Create: `tests/analysis/__init__.py` (empty)
- Create: `tests/analysis/test_schemas.py` (death + enum-coverage
  + Pydantic round-trip tests)

## Death Test Requirements

Write these BEFORE implementation. Each must be red against an empty
`schemas.py`, then green after implementation.

- **DT-1.1** — Constructing `BehaviorFlag(flag_type="bogus_type", ...)`
  raises `pydantic.ValidationError` naming the bad enum value.
- **DT-1.2** — Constructing `BehaviorFlag(confidence="kinda", ...)`
  raises `pydantic.ValidationError`.
- **DT-1.3** — Constructing `Directive(status="frozen", ...)` raises
  `pydantic.ValidationError`.
- **DT-1.4** — Constructing `ToolUseSpan(success=True, duration_ms=None, ...)`
  raises `pydantic.ValidationError` — succeeded-but-unknown-duration
  is incoherent.
- **DT-1.5** — `BehaviorFlagType` enum has exactly 6 values, exact
  strings: `unnecessary_read`, `redundant_exploration`, `missed_shortcut`,
  `repeated_operation`, `wrong_tool_choice`,
  `excessive_context_gathering`. (Per SD §5.5.1.)
- **DT-1.6** — `DirectiveStatus` enum has exactly 5 values, exact
  strings: `active`, `disabled`, `expired`, `superseded`, `obsolete`.
- **DT-1.7** — `SegmentData(user_prompt=None, events=[])` is valid
  (pre-prompt empty segment must round-trip).

## Implementation Steps

- [ ] Step 1: Write tests/analysis/test_schemas.py with DT-1.1..DT-1.7
      plus enum-coverage tests (one per enum value) plus 2 round-trip
      tests (BehaviorFlag construct + serialize + deserialize equals
      original; Directive same).
- [ ] Step 2: Run tests — all red.
- [ ] Step 3: Write `src/secondsight/analysis/schemas.py` per the spec
      below. Use `pydantic.BaseModel`, `model_config =
      ConfigDict(extra="forbid")` to reject unknown fields.
- [ ] Step 4: Write `src/secondsight/analysis/__init__.py` re-exporting
      every public symbol.
- [ ] Step 5: Run tests — all green.
- [ ] Step 6: Run full test suite — no regressions.
- [ ] Step 7: Write scar report. Commit.

## Spec — `src/secondsight/analysis/schemas.py`

```python
"""Analysis-layer Pydantic contracts and enums (GUR-100 task-1).

This is the SINGLE SOURCE OF TRUTH for the BehaviorFlagType vocabulary
(SD §5.5.1) and the data shapes the analysis prompt builder (GUR-101)
and the Phase-2 repositories consume.

Death cases enforced here (Pydantic v2 validation):
- BehaviorFlag.flag_type rejects values outside BehaviorFlagType.
- BehaviorFlag.confidence rejects values outside {high, medium, low}.
- Directive.status rejects values outside DirectiveStatus.
- ToolUseSpan rejects (success=True, duration_ms=None) — succeeded-but-
  unknown-duration is incoherent.

Repositories MUST re-validate via the same enums on insert because
`model_construct()` bypasses Pydantic.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator


# ---------- Enums (string-valued for direct DB persistence) ----------


class BehaviorFlagType(str, Enum):
    """Per SD §5.5.1 — single source of truth for the analysis prompt.

    Six values. Adding a value requires a co-modified SD §5.5.1 update.
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
    """Per SD §5.5.2 (with `confidence` field added — D3 SD patch)."""

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
    """Per SD §7.4 (with `disabled_at`, `disabled_reason` added — D3)."""

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
    """Paired tool_use_start/end. Death case: success=True with
    duration_ms=None is incoherent and rejected.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    target: str | None
    success: bool | None  # None = orphan start (no matching end)
    duration_ms: int | None
    start_seq: int
    end_seq: int | None  # None = orphan start
    metadata: dict[str, object] = {}

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
    """

    model_config = ConfigDict(extra="forbid")

    segment_index: int
    user_prompt: dict | None
    events: list[dict | ToolUseSpan]
    session_id: str
    project_id: str


class SegmentMetrics(TypedDict):
    """Per SD §5.3.1 step 2. Pure-function output of metrics.compute_segment_metrics."""

    total_tokens: int
    unique_files: int
    duration: float  # seconds
    error_count: int
```

## Spec — `src/secondsight/analysis/__init__.py`

```python
"""Analysis layer — Phase 2 data contracts and read-side modules (GUR-100)."""

from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
    SegmentData,
    SegmentMetrics,
    ToolUseSpan,
)

__all__ = [
    "BehaviorFlag",
    "BehaviorFlagType",
    "Directive",
    "DirectiveStatus",
    "DirectiveType",
    "SegmentData",
    "SegmentMetrics",
    "ToolUseSpan",
]
```

(Add `Segmenter` and `compute_segment_metrics` to `__all__` in
task-4 / task-5 respectively.)

## Expected Scar Report Items

- Potential shortcut: defining `BehaviorFlag.confidence` as
  `str` instead of `Literal["high","medium","low"]` "to avoid
  Pydantic friction" — rejected; this is the death case D1 closes.
- Potential shortcut: skipping the `success=True + duration_ms=None`
  validator on `ToolUseSpan` because "the segmenter won't construct
  that case" — rejected; the model is also constructed by GUR-101's
  prompt-output parser, which can absolutely produce that case if
  the LLM hallucinates.
- Assumption to verify: `pyproject.toml`'s pydantic version is v2.
  If v1, the `model_validator` decorator and `ConfigDict` need to
  be replaced with v1 equivalents — flag and ask before proceeding.

## Acceptance Criteria

Covers the following acceptance.yaml scenarios:
- "Silent failure - flag_type drift via Pydantic bypass" (Pydantic
  side; repo defensive guard is task-2)
- "Silent failure - directive status drift via model_construct
  bypass" (Pydantic side; repo defensive guard is task-3)
- "Unknown outcome - tool execution outcome not recorded" (the
  `success=None` shape is defined here; segmenter usage is task-4)
