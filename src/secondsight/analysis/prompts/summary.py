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

NOTE: the [任務 / Tone / Audience] block in this prompt is intentionally
left as a TODO for the project lead. The substance of the summary
(who is reading? what should they leave with? how prescriptive?) is a
UX decision that the prompt-builder author cannot make in isolation.
See `_TASK_BLOCK` below for the placeholder + guidance.
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


# ----------------------------------------------------------------------
# TODO(yuyu_liao): replace _TASK_BLOCK below with the dashboard-facing
# session-summary instructions. This is where the UX decision lives.
#
# Open questions only the project lead should answer:
#   1. Audience tone — neutral / encouraging / blunt? (e.g., "Read 3
#      unrelated files" vs "Could have skipped 3 file reads")
#   2. Headline length & shape — strictly one sentence? Should it lead
#      with a verdict ("Efficient session, 1 issue") or a count
#      ("3 flags across 5 segments")?
#   3. key_findings ordering — by frequency, by severity (high
#      confidence first), or chronological?
#   4. Should body call out specific event_ids (linkable in dashboard)
#      or stay descriptive ("the README read in segment 2")?
#   5. Should low-confidence flags be summarized or omitted?
#
# Write 5-10 lines of prompt instructions that capture your answer.
# Format: prose for the LLM, in the same Chinese-prose register as
# _SYSTEM_BLOCK above. The {segments_json} placeholder will receive
# the SegmentAnalysis list serialized as JSON.
#
# When you replace _TASK_BLOCK, also revisit SummaryOutput field
# constraints if your decision changes them (e.g., max_length on
# key_findings, body length, or headline character cap).
# ----------------------------------------------------------------------
_TASK_BLOCK = (
    "TODO(yuyu_liao): replace this placeholder with dashboard-facing\n"
    "summary instructions. See module-level guidance.\n\n"
    "Provisional fallback (so prompt remains buildable during dev): \n"
    "請根據以下 segments 產出 session 行為摘要。"
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
