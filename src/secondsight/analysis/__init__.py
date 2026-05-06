"""Analysis layer — Phase 2 data contracts, read-side modules, and
prompt builders (GUR-100 + GUR-101).

Public surface:
- Pydantic schemas / enums (task-1): BehaviorFlag, BehaviorFlagDraft,
  BehaviorFlagType, Directive, DirectiveStatus, DirectiveType,
  FLAG_DEFINITIONS, FlagDefinition, SegmentAnalysis, SegmentData,
  SegmentMetrics, ToolUseSpan.
- Read-side assembly (task-4): Segmenter.
- Pure-function metrics (task-5): compute_segment_metrics.
- Prompt builders (GUR-101 P2-5/6/7): see secondsight.analysis.prompts.
"""

from secondsight.analysis.metrics import compute_segment_metrics
from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    BehaviorFlag,
    BehaviorFlagDraft,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
    FlagDefinition,
    SegmentAnalysis,
    SegmentData,
    SegmentMetrics,
    ToolUseSpan,
)
from secondsight.analysis.segmenter import Segmenter

__all__ = [
    "FLAG_DEFINITIONS",
    "BehaviorFlag",
    "BehaviorFlagDraft",
    "BehaviorFlagType",
    "Directive",
    "DirectiveStatus",
    "DirectiveType",
    "FlagDefinition",
    "SegmentAnalysis",
    "SegmentData",
    "SegmentMetrics",
    "Segmenter",
    "ToolUseSpan",
    "compute_segment_metrics",
]
