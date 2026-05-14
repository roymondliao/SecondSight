"""P2-7 — session report summary prompt (GUR-101 task P2-7).

Renders a per-session behavior summary for the dashboard (GUR-106)
via the jinja2 template at `prompts/analysis/summary.jinja2`
(Task 3 refactor).

The jinja2 environment uses StrictUndefined — any missing context
variable raises jinja2.UndefinedError at render time (DC9 protection).

Input: list of SegmentAnalysis (one per segment, output of P2-5).
Output: SummaryOutput with three zoom levels:
- `headline` — single-sentence TL;DR for the dashboard card.
- `key_findings` — 0-5 bulleted actionable observations (list view).
- `body` — 2-4 paragraph narrative (detail view).

Death cases (unchanged from pre-refactor):
- Empty `segments` is rendered as-is; the LLM should produce
  headline="No segments analyzed" and empty key_findings/body.
- key_findings is bounded ≤ 5 by the prompt and re-validated by
  Pydantic via Field(max_length=5).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from secondsight.analysis.schemas import SegmentAnalysis
from secondsight.prompts._loader import render


class SummaryOutput(BaseModel):
    """Three-zoom-level dashboard summary for one session."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=200)
    key_findings: list[str] = Field(max_length=5)
    body: str


def build_summary_prompt(
    session_id: str,
    project_id: str,
    segments: list[SegmentAnalysis],
) -> str:
    """Render the dashboard summary prompt for one session via jinja2.

    Pure function. session_id and project_id are passed through into
    the rendered context block so the LLM can reference them in the
    output if useful (e.g., for cross-session comparison phrasing).

    The jinja2 template is loaded from
    `src/secondsight/prompts/analysis/summary.jinja2`.
    StrictUndefined ensures any missing context variable raises
    UndefinedError immediately (DC9 protection).
    """
    segments_dump = [s.model_dump(mode="json") for s in segments]
    segments_json = json.dumps(segments_dump, ensure_ascii=False, sort_keys=True, indent=2)
    return render(
        "analysis/summary",
        context={
            "session_id": session_id,
            "project_id": project_id,
            "segment_count": len(segments),
            "segments_json": segments_json,
        },
    )
