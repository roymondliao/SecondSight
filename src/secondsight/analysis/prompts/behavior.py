"""P2-5 — segment-level analysis prompt (GUR-101 task P2-5).

Renders the SD §5.5.2 prompt via the jinja2 template at
`prompts/analysis/behavior.jinja2` (Task 3 refactor).

The jinja2 environment uses StrictUndefined — any missing context
variable raises jinja2.UndefinedError at render time (DC9 protection).

Death cases (unchanged from pre-refactor):
- Every `BehaviorFlagType` enum member MUST appear in the rendered
  prompt's [Flag Type Definitions] section. The render call constructs
  the flag_definitions_block by iterating the enum; a missing dict
  entry raises KeyError at build time rather than silently omitting.
- The output format block names every required JSON field, including
  `confidence` (Literal["high", "medium", "low"]).
- No pre-truncation. SegmentData of any size is rendered as-is.

Output validation: `SegmentAnalysis` (in `secondsight.analysis.schemas`)
validates the LLM's JSON output.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from secondsight.analysis.output import AnalysisOutput
from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    BehaviorFlagType,
    SegmentData,
    SegmentMetrics,
)
from secondsight.prompts._loader import render


def render_flag_definitions() -> str:
    """Render the per-flag-type definition entries by iterating BehaviorFlagType.

    Iterates the enum (not FLAG_DEFINITIONS keys) so a missing dict entry
    surfaces as KeyError immediately rather than silently omitting that
    flag type from the rendered prompt.

    Returns a string suitable for use as the `flag_definitions_block`
    context variable in the behavior jinja2 template. The surrounding
    header text is provided by the template itself; this function
    returns only the per-type definition entries.
    """
    lines: list[str] = []
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
        if isinstance(ev, BaseModel):
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
    """Render the full SD §5.5.2 prompt for one segment via jinja2.

    Pure function: same (segment, metrics) pair always renders the
    same string. JSON dump uses sort_keys=True for determinism, which
    matters for golden-file tests and for prompt-caching strategies in
    the orchestrator.

    The jinja2 template is loaded from
    `src/secondsight/prompts/analysis/behavior.jinja2`.
    StrictUndefined ensures any missing context variable raises
    UndefinedError immediately (DC9 protection).
    """
    payload = _segment_payload(segment, metrics)
    segment_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    flag_definitions_block = render_flag_definitions()
    analysis_output_schema = json.dumps(AnalysisOutput.model_json_schema(), indent=2)
    return render(
        "analysis/behavior",
        context={
            "segment_json": segment_json,
            "flag_definitions_block": flag_definitions_block,
            "analysis_output_schema": analysis_output_schema,
        },
    )
