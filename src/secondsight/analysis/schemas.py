"""Analysis-layer Pydantic contracts and enums (GUR-100 task-1; GUR-101
extension adds FLAG_DEFINITIONS, BehaviorFlagDraft, SegmentAnalysis).

Single source of truth for the BehaviorFlagType vocabulary (SD §5.5.1),
the FLAG_DEFINITIONS content rendered into the SD §5.5.2 prompt, and
the data shapes the analysis prompt builder (GUR-101) and the Phase-2
repositories consume.

Death cases enforced here (Pydantic v2 validation):
- BehaviorFlag.flag_type rejects values outside BehaviorFlagType.
- BehaviorFlag.confidence rejects values outside {high, medium, low}.
- BehaviorFlagDraft mirrors the LLM-emitted shape; the orchestrator
  promotes Draft → BehaviorFlag by injecting persistence fields.
- SegmentAnalysis validates the full SD §5.5.2 prompt output JSON.
- Directive.status rejects values outside DirectiveStatus.
- ToolUseSpan rejects (success=True, duration_ms=None) — a successful
  span MUST carry a measured duration; orphan starts use success=None.

Repositories MUST re-validate via the same enums on insert because
`model_construct()` bypasses Pydantic. That defensive guard lives in
behavior_flags_repository.py and directives_repository.py (D1).

SD references:
- §5.5.1 — BehaviorFlagType vocabulary (six values) + FLAG_DEFINITIONS.
- §5.5.2 — segment-level analysis prompt; `confidence` field is added
  to the output schema in the same PR (D3 ship gate).
- §7.4   — directives DDL; `disabled_at` / `disabled_reason` columns
  are added in the same PR (D3 ship gate).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class BehaviorFlagDraft(BaseModel):
    """The exact shape the LLM emits per SD §5.5.2 output JSON.

    Promoted to `BehaviorFlag` by the GUR-102 orchestrator, which
    injects `id`, `project_id`, `session_id`, `segment_index`,
    `intent_summary`, and `created_at`. Keeping the LLM-output shape
    distinct prevents the death case where Pydantic validation rejects
    a syntactically valid model response just because it lacks fields
    the model could not have known.
    """

    model_config = ConfigDict(extra="forbid")

    flag_type: BehaviorFlagType
    event_ids: list[str]
    reason: str
    confidence: Literal["high", "medium", "low"]


class SegmentAnalysis(BaseModel):
    """Per SD §5.5.2 — the wrapping object the LLM returns for one segment.

    Validates the entire prompt output. `total_events` and
    `flagged_events` are LLM-reported counts; the orchestrator should
    cross-check `len(flags) == flagged_events` and log a divergence
    warning on mismatch (kept out of schema validation so a small
    counting discrepancy does not throw away an otherwise-good
    analysis batch).
    """

    model_config = ConfigDict(extra="forbid")

    segment_summary: str
    flags: list[BehaviorFlagDraft]
    total_events: int
    flagged_events: int


class Directive(BaseModel):
    """Per SD §7.4 (with `disabled_at` / `disabled_reason` added — D3;
    `identity_key` added — GUR-102 task-1).

    Lifecycle status mutates over time via DirectivesRepository.update_status.

    `identity_key`: stable hash for UPSERT-based deduplication. Computed by
    `secondsight.analysis.aggregator.compute_identity_key`. See that function's
    docstring for the exact hash inputs and rationale. Empty string is rejected
    by DirectivesRepository._guard; the DDL server_default="" is a transitional
    migration default only and is never valid for real rows.
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
    identity_key: str = ""
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


# ---------- GUR-102 Phase 2 orchestration contracts ----------


class AnalysisRunStage(str, Enum):
    """Audit stages for an analysis pipeline run (analysis_runs.stage).

    Validation lives at the repository layer (D1 — mirrors GUR-100's
    behavior_flags.flag_type pattern). No DB CHECK constraint.

    Terminal stages: summary_written, aggregated, failed.
    Non-terminal stages: pending, segmented, behavior_done.
    """

    PENDING = "pending"
    SEGMENTED = "segmented"
    BEHAVIOR_DONE = "behavior_done"
    SUMMARY_WRITTEN = "summary_written"
    AGGREGATED = "aggregated"
    FAILED = "failed"


# Terminal stages where the pipeline has reached a definitive end state.
# Canonical single source of truth — imported by AnalysisRunsRepository.
# When adding a new terminal stage, update ONLY this set; the repository
# derives _NON_TERMINAL_STAGES from it automatically.
TERMINAL_STAGES: frozenset[str] = frozenset(
    {
        AnalysisRunStage.SUMMARY_WRITTEN.value,
        AnalysisRunStage.AGGREGATED.value,
        AnalysisRunStage.FAILED.value,
    }
)


class AnalysisRun(BaseModel):
    """Audit record for one analysis pipeline run (GUR-102 task-1).

    Persisted to the `analysis_runs` table. One row per pipeline
    execution. The row is inserted at stage='pending' BEFORE any
    pipeline work begins so a SIGKILL after insertion still leaves an
    audit trail (DC-1).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    session_id: str
    stage: AnalysisRunStage
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    flags_inserted: int = Field(default=0, ge=0)


class SessionReport(BaseModel):
    """Artifact record for the analysis output for one session (GUR-102 task-1).

    Persisted to the `session_reports` table with UNIQUE(session_id).
    Upserted (not inserted) to allow re-runs to update the report.

    `key_findings` is a list of strings persisted as JSON. `max_length=5`
    matches the `prompts/summary.py:43` constraint — drift requires
    co-modification.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    session_id: str
    analysis_run_id: str
    headline: str = Field(min_length=1, max_length=200)
    key_findings: list[str] = Field(max_length=5)
    body: str
    created_at: datetime
    updated_at: datetime


# ---------- Shared vocabulary constants ----------

# Single source of truth for the valid confidence values used in
# BehaviorFlag.confidence (Literal) and in the defensive _guard validators.
# Co-located here with BehaviorFlagType so that adding a new confidence level
# requires ONE edit (this set) and imports in behavior.py +
# behavior_flags_repository.py pick it up automatically.
# Exported via analysis/__init__.py.
VALID_CONFIDENCE: frozenset[str] = frozenset({"high", "medium", "low"})


# ---------- Vocabulary content (SD §5.5.1) ----------


class FlagDefinition(TypedDict):
    """Per SD §5.5.1. Three fields render directly into the prompt's
    `[Flag Type 定義]` section so the LLM sees one block per flag type.
    """

    description: str
    criteria: str
    example: str


# Source-of-truth content for SD §5.5.1. The prompt builder MUST iterate
# over BehaviorFlagType (not over this dict's keys) so a typo here
# surfaces as a KeyError at prompt-build time rather than silently
# dropping a flag type from the rendered prompt.
FLAG_DEFINITIONS: dict[BehaviorFlagType, FlagDefinition] = {
    BehaviorFlagType.UNNECESSARY_READ: {
        "description": "讀了跟當前任務意圖無關的檔案",
        "criteria": "該檔案的內容與 user prompt 的意圖無直接關聯",
        "example": "User 要求修改 a.py，agent 先讀了 README.md",
    },
    BehaviorFlagType.REDUNDANT_EXPLORATION: {
        "description": "已經有足夠資訊完成任務，仍在做額外探索",
        "criteria": ("agent 已具備完成任務所需的資訊，卻繼續 ls / grep / read 不相關的路徑"),
        "example": "User 給了明確路徑，agent 還在 ls 整個目錄結構",
    },
    BehaviorFlagType.MISSED_SHORTCUT: {
        "description": "有更直接的路徑可達成目標但沒走",
        "criteria": "存在更短的操作路徑，agent 選了迂迴的方式",
        "example": "User 給了檔名，agent 卻用 grep 搜尋整個 codebase 才找到",
    },
    BehaviorFlagType.REPEATED_OPERATION: {
        "description": "在同一 segment 內重複做同樣的操作",
        "criteria": ("相同的 tool + target 組合在同一 segment 出現多次且無合理原因"),
        "example": "同一個 segment 內讀了同一個檔案兩次",
    },
    BehaviorFlagType.WRONG_TOOL_CHOICE: {
        "description": "使用了不適合當前任務的工具",
        "criteria": "存在更適合的工具但 agent 選了效率較低的替代方案",
        "example": "該用 grep 搜尋關鍵字，卻逐個 read 檔案找內容",
    },
    BehaviorFlagType.EXCESSIVE_CONTEXT_GATHERING: {
        "description": "任務規模不需要大量 context，agent 卻收集了過多資訊",
        "criteria": ("簡單任務（如單檔 edit）卻讀了大量不相關的檔案建立 context"),
        "example": "簡單 bug fix 卻讀了十幾個不相關的檔案",
    },
}
