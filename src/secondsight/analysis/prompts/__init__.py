"""Prompt builders for the two-layer analysis system (GUR-101).

Public surface:
- P2-5 (behavior.py): build_segment_prompt — renders the SD §5.5.2
  segment-level prompt; output validated by SegmentAnalysis.
- P2-6 (aggregate.py): build_aggregate_prompt — renders the SD §5.5.3
  per-flag-type aggregation prompt; output validated by AggregateOutput.
- P2-7 (summary.py): build_summary_prompt — renders the per-session
  behavior report summary for the dashboard.

All builders are pure functions: same inputs → same string. They never
truncate input (span splitting is the GUR-102 orchestrator concern per
SD §5.3.3) and never call the network. The orchestrator owns model
choice, retries, and parsing.
"""

from secondsight.analysis.prompts.aggregate import (
    AggregateOutput,
    AggregatePattern,
    FlagSummary,
    build_aggregate_prompt,
)
from secondsight.analysis.prompts.behavior import build_segment_prompt
from secondsight.analysis.prompts.summary import (
    SummaryOutput,
    build_summary_prompt,
)

__all__ = [
    "AggregateOutput",
    "AggregatePattern",
    "FlagSummary",
    "SummaryOutput",
    "build_aggregate_prompt",
    "build_segment_prompt",
    "build_summary_prompt",
]
