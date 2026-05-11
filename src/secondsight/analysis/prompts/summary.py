"""P2-7 — session report summary prompt (GUR-101 task P2-7).

Renders a per-session behavior summary for the dashboard (GUR-106).
Input: list of SegmentAnalysis (one per segment, output of P2-5).
Output: SummaryOutput with three zoom levels:
- `headline` — single-sentence TL;DR for the dashboard card.
- `key_findings` — 0-5 bulleted actionable observations (list view).
- `body` — 2-4 paragraph narrative (detail view).

Death cases:
- Empty `segments` (a session with no analyzed segments) is rendered
  as-is; the LLM should produce headline="No segments analyzed" and
  empty key_findings/body. The prompt does not pre-empty-check; that
  filter is orchestrator concern.
- key_findings is bounded ≤ 5 by the prompt and re-validated by
  Pydantic via Field(max_length=5). A larger list signals the LLM
  ignored the spec; failing validation is preferable to silently
  truncating.

UX defaults encoded in `_TASK_BLOCK`: neutral observational tone,
verdict+count headline, confidence-then-frequency ordering for
key_findings, event_id references in body, low-confidence flags
summarized as a count. Each default ties to a prior ratified
decision — see the comment block above `_TASK_BLOCK` for the
mapping. Revising any of these is a one-line change.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from secondsight.analysis.schemas import SegmentAnalysis


class SummaryOutput(BaseModel):
    """Three-zoom-level dashboard summary for one session."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=200)
    key_findings: list[str] = Field(max_length=5)
    body: str


_SYSTEM_BLOCK = (
    "你是 coding agent 行為分析報告撰稿人。你的任務是把單一 session 的"
    "行為標記彙整成一份 dashboard 可閱讀的摘要。"
)

_SCHEMA_BLOCK = (
    "輸入是該 session 所有 segment 的分析結果（list[SegmentAnalysis]）：\n"
    "- segment_summary: 該 segment 的整體表現一句話評價\n"
    "- flags: 此 segment 內被標記的 behavior flags（含 flag_type / "
    "event_ids / reason / confidence）\n"
    "- total_events / flagged_events: 該 segment 的事件數與被標記數"
)


# UX decisions encoded below (board accepted "keep doing the task" delegation
# 2026-05-06; the documented open questions in the GUR-101 thread were
# answered by these defaults, all derived from prior ratified decisions):
#
#   - Tone: neutral observational. Per SD §5.3.1, the dashboard is for the
#     user to reflect on behavior, not for the system to coach. Describe
#     what happened; do not prescribe.
#   - Headline shape: verdict + count, single sentence ≤ 200 chars.
#     The verdict gives at-a-glance read; the count anchors the verdict
#     in evidence and avoids judgmental phrasing.
#   - key_findings ordering: by confidence first (high → medium → low),
#     then by frequency. High-confidence flags are the trustworthy signal
#     per the GUR-100 "drop-low is orchestrator policy" ruling — surfacing
#     them first matches that hierarchy.
#   - Body specificity: reference event_ids verbatim. The dashboard
#     (GUR-106) is designed to hyperlink event_ids to trace detail —
#     descriptive paraphrasing would lose that affordance.
#   - Low-confidence flags: summarize as a count line, never enumerate.
#     They may be noise; surfacing each one would dilute the report's
#     signal-to-noise ratio.
_TASK_BLOCK = (
    "請根據以下 segments 產出此 session 的行為摘要，分三個層級填入 JSON 欄位：\n\n"
    "1. headline（≤ 200 字元，一句話）：先給整體評價（例如「整體效率良好」"
    "或「多處不必要操作」），接著用「N flags across M segments」格式給出"
    "事件數量，作為評價的證據錨點。語氣保持中性觀察，不勸導、不批判。\n\n"
    "2. key_findings（最多 5 條，每條 1-2 句，可操作）：依 confidence 由高到低"
    "排序，同 confidence 內依出現次數由多到少排序。優先呈現 confidence=high 的"
    "flags；若名額仍有剩餘再納入 medium。每條描述「在哪個 segment 做了什麼，"
    "為何低效」即可，不需要建議改進方式（dashboard 由使用者自行解讀）。\n\n"
    "3. body（2-4 段完整敘述）：依時間順序敘述 session 整體行為走向，並在"
    "提到具體事件時直接引用 event_ids（dashboard 會把這些 ID 連結到 trace 詳情）。"
    "Confidence=low 的 flags 不要逐一列出，僅在最後加一句「另有 N 條低信心觀察」"
    "（N=0 時整句省略）。"
)


_OUTPUT_FORMAT_BLOCK = (
    "回傳 JSON：\n"
    "{\n"
    '  "headline": "一句話的 dashboard 標題（≤ 200 字元）",\n'
    '  "key_findings": ["最多 5 條可操作觀察，每條 1-2 句"],\n'
    '  "body": "完整段落式說明（2-4 段）"\n'
    "}"
)


def build_summary_prompt(
    session_id: str,
    project_id: str,
    segments: list[SegmentAnalysis],
) -> str:
    """Render the dashboard summary prompt for one session.

    Pure function. session_id and project_id are passed-through into
    the rendered context block so the LLM can reference them in the
    output if useful (e.g., for cross-session comparison phrasing).
    """
    segments_dump = [s.model_dump(mode="json") for s in segments]
    segments_json = json.dumps(segments_dump, ensure_ascii=False, sort_keys=True, indent=2)
    context_block = (
        f"session_id: {session_id}\nproject_id: {project_id}\nsegment_count: {len(segments)}"
    )
    return (
        f"[System]\n{_SYSTEM_BLOCK}\n\n"
        f"[Schema 說明]\n{_SCHEMA_BLOCK}\n\n"
        f"[Session Context]\n{context_block}\n\n"
        f"[任務]\n{_TASK_BLOCK}\n\n"
        f"[Segments]\n{segments_json}\n\n"
        f"[Output Format]\n{_OUTPUT_FORMAT_BLOCK}"
    )
