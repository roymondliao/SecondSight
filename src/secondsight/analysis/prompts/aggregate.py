"""P2-6 — cross-session aggregation prompt (GUR-101 task P2-6).

Renders the SD §5.5.3 Step-2 LLM prompt: ONE call per flag_type that
clusters semantically-similar flag occurrences and produces convention
text for each cluster. Step 1 (group-by) and Step 3 (top-N selection
across flag types) are automated and live in the GUR-102 orchestrator.

Death cases:
- The flag_type passed must be a BehaviorFlagType enum member; passing
  a string would render an unconstrained label into the prompt and
  invite drift from the SD vocabulary.
- The convention spec ("≤ 200 tokens, 2-5 句, actionable") is rendered
  verbatim into the prompt's task block so the LLM cannot interpret
  "convention" as freeform prose.
- AggregatePattern.occurrence_count is constrained to ≥ 1; a pattern
  with zero occurrences is incoherent and would survive a permissive
  parser silently if not validated.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    BehaviorFlagType,
)


class FlagSummary(BaseModel):
    """Tight input shape for one prior flag occurrence.

    Compared to the full BehaviorFlag row, FlagSummary drops
    persistence fields (id, project_id, segment_index, created_at,
    confidence) — the aggregation prompt does not benefit from them
    and including them inflates input tokens. session_id is kept so
    the LLM can populate AggregatePattern.representative_sessions.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    segment_summary: str
    reason: str


class AggregatePattern(BaseModel):
    """One discovered behavior pattern within a single flag_type."""

    model_config = ConfigDict(extra="forbid")

    pattern_description: str
    occurrence_count: int = Field(ge=1)
    representative_sessions: list[str]
    convention: str


class AggregateOutput(BaseModel):
    """LLM output for one flag_type aggregation call (SD §5.5.3 Step 2)."""

    model_config = ConfigDict(extra="forbid")

    patterns: list[AggregatePattern]


_SYSTEM_BLOCK = (
    "你是 coding agent 行為模式分析專家。你的任務是從多個 session 的行為"
    "標記中歸納出行為模式，並產出行為準則。"
)

_TASK_TEMPLATE = (
    '以下是多個 session 中被標記為 "{flag_type_value}" 的行為記錄。\n'
    "({flag_type_description})\n\n"
    "請：\n"
    "1. 依據 segment_summary 和 reason 做語意歸類，辨識出不同的行為模式\n"
    "   （同一 flag_type 下可能有多種不同的行為模式）\n"
    "2. 統計每個行為模式的出現次數\n"
    "3. 為每個行為模式產出一條自然語言的行為準則（convention）\n"
    "   - Convention 必須精煉（2-5 句，≤ 200 tokens）\n"
    "   - Convention 必須是可操作的指導，不是抽象原則"
)

_OUTPUT_FORMAT_BLOCK = (
    "回傳 JSON：\n"
    "{\n"
    '  "patterns": [\n'
    "    {\n"
    '      "pattern_description": "此行為模式的描述",\n'
    '      "occurrence_count": number,\n'
    '      "representative_sessions": ["貢獻此模式的 session IDs"],\n'
    '      "convention": "產出的行為準則文字"\n'
    "    }\n"
    "  ]\n"
    "}"
)


def build_aggregate_prompt(flag_type: BehaviorFlagType, flags: list[FlagSummary]) -> str:
    """Render the SD §5.5.3 Step-2 prompt for ONE flag_type group.

    The orchestrator calls this once per flag_type whose group is
    non-empty. Empty groups (no flags of that type across the session
    history) do NOT call the LLM — that filter is orchestrator policy
    and is not enforced here, but passing flags=[] to this builder
    still produces a syntactically valid prompt; the LLM will return
    patterns=[] for an empty input.
    """
    if not isinstance(flag_type, BehaviorFlagType):
        raise TypeError(f"flag_type must be BehaviorFlagType, got {type(flag_type).__name__}")
    flag_type_description = FLAG_DEFINITIONS[flag_type]["description"]
    task_block = _TASK_TEMPLATE.format(
        flag_type_value=flag_type.value,
        flag_type_description=flag_type_description,
    )
    flags_json = json.dumps(
        [f.model_dump(mode="json") for f in flags],
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (
        f"[System]\n{_SYSTEM_BLOCK}\n\n"
        f"[任務]\n{task_block}\n\n"
        f"[Behavior Flags]\n{flags_json}\n\n"
        f"[Output Format]\n{_OUTPUT_FORMAT_BLOCK}"
    )
