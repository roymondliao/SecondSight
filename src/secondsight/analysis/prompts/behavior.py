"""P2-5 — segment-level analysis prompt (GUR-101 task P2-5).

Renders the SD §5.5.2 prompt verbatim from `BehaviorFlagType` +
`FLAG_DEFINITIONS` + a `SegmentData` payload + supplementary
`SegmentMetrics`. The orchestrator (GUR-102) owns model invocation,
retries, and JSON parsing; this module is pure-function string
construction.

Death cases:
- Every `BehaviorFlagType` enum member MUST appear in the rendered
  prompt's [Flag Type 定義] section. The renderer iterates the enum
  (not the FLAG_DEFINITIONS dict) so a missing dict entry raises
  KeyError at build time rather than silently omitting a flag type.
- The output format block names every required JSON field, including
  `confidence` (Literal["high", "medium", "low"]), so the LLM cannot
  omit it on a well-formed response.
- No pre-truncation. SegmentData of any size is rendered as-is;
  span splitting is the GUR-102 orchestrator concern (SD §5.3.3).

Output validation: `SegmentAnalysis` (in
`secondsight.analysis.schemas`) validates the LLM's JSON output.
"""

from __future__ import annotations

import json
from typing import Any

from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    BehaviorFlagType,
    SegmentData,
    SegmentMetrics,
)


_SYSTEM_BLOCK = (
    "你是 coding agent 行為分析專家。你的任務是分析 agent 在回應 user prompt 時的操作效率。"
)

_SCHEMA_BLOCK = (
    "以下是 segment 資料的 field 定義：\n"
    "- user_prompt: agent 收到的使用者指令，是判斷所有操作是否必要的基準\n"
    "- events: 按時間排序的事件序列\n"
    "  - thinking: agent 的推理步驟（token_count 反映推理深度，"
    "duration_ms 反映推理時間）\n"
    "  - tool_use_start: 工具操作開始（tool_name, target, metadata）\n"
    "  - tool_use_end: 工具操作結束（tool_name, target, success, "
    "duration_ms）\n"
    "  - sub_agent_start/end: 子 agent 呼叫的開始與結束\n"
    "  - response: agent 回覆使用者（token_count, has_code_block）\n"
    "- supplementary_metrics: 輔助統計，僅供參考，不作為獨立判斷依據"
)

_TASK_BLOCK = (
    "分析此 segment 中每個 event 是否為達成 user prompt 意圖的必要操作。\n"
    "對不必要或低效的操作標記 behavior flag。\n"
    "注意：只有你確信該操作不必要時才標記，不確定時不標記。"
)

# Output format block — explicit about every required field, including
# `confidence`. The orchestrator parses this through SegmentAnalysis;
# missing fields fail validation rather than coerce silently.
_OUTPUT_FORMAT_BLOCK = (
    "回傳 JSON：\n"
    "{\n"
    '  "segment_summary": "對此 segment agent 整體表現的一句話評價",\n'
    '  "flags": [\n'
    "    {\n"
    '      "flag_type": "必須是上述定義的合法類型",\n'
    '      "event_ids": ["涉及的事件 ID"],\n'
    '      "reason": "為什麼判定為低效（一句話）",\n'
    '      "confidence": "high | medium | low — 你對此 flag 判定的信心度"\n'
    "    }\n"
    "  ],\n"
    '  "total_events": number,\n'
    '  "flagged_events": number\n'
    "}"
)


def render_flag_definitions() -> str:
    """Render the [Flag Type 定義] block by iterating BehaviorFlagType.

    Iterates the enum, not FLAG_DEFINITIONS, so a missing dict entry
    (e.g., a future enum member without a corresponding definition)
    raises KeyError immediately rather than silently omitting that
    flag type from the rendered prompt.
    """
    lines = ["以下是所有合法的 behavior flag 類型，你只能使用這些類型：\n"]
    for flag_type in BehaviorFlagType:
        defn = FLAG_DEFINITIONS[flag_type]
        lines.append(f"- {flag_type.value}")
        lines.append(f"  description: {defn['description']}")
        lines.append(f"  criteria: {defn['criteria']}")
        lines.append(f"  example: {defn['example']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _segment_payload(segment: SegmentData, metrics: SegmentMetrics) -> dict[str, Any]:
    """Assemble the JSON payload the LLM will analyze.

    `events` is rendered as the union list (ToolUseSpan |
    raw-event-dict) the segmenter produced. ToolUseSpan is dumped via
    Pydantic's model_dump so its tagged success/duration semantics
    survive serialization. user_prompt may be None (pre-prompt segment
    per SD §5.3.1) — the LLM is told this in the schema block.
    """
    events_dump: list[Any] = []
    for ev in segment.events:
        if hasattr(ev, "model_dump"):
            events_dump.append(ev.model_dump(mode="json"))
        else:
            events_dump.append(ev)
    return {
        "segment_index": segment.segment_index,
        "user_prompt": segment.user_prompt,
        "events": events_dump,
        "supplementary_metrics": dict(metrics),
    }


def build_segment_prompt(segment: SegmentData, metrics: SegmentMetrics) -> str:
    """Render the full SD §5.5.2 prompt for one segment.

    Pure function: same (segment, metrics) pair always renders the
    same string. JSON dump uses sort_keys=True for determinism, which
    matters for golden-file tests and for prompt-caching strategies in
    the orchestrator.
    """
    payload = _segment_payload(segment, metrics)
    segment_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        f"[System]\n{_SYSTEM_BLOCK}\n\n"
        f"[Schema 說明]\n{_SCHEMA_BLOCK}\n\n"
        f"[Flag Type 定義]\n{render_flag_definitions()}\n\n"
        f"[任務]\n{_TASK_BLOCK}\n\n"
        f"[Segment Data]\n{segment_json}\n\n"
        f"[Output Format]\n{_OUTPUT_FORMAT_BLOCK}"
    )
