"""Analysis layer — Phase 2 data contracts and read-side modules (GUR-100).

Public surface:
- Pydantic schemas / enums (task-1): BehaviorFlag, BehaviorFlagType,
  Directive, DirectiveStatus, DirectiveType, SegmentData, SegmentMetrics,
  ToolUseSpan.
- Read-side assembly (task-4): Segmenter — populated when task-4 lands.
- Pure-function metrics (task-5): compute_segment_metrics — populated
  when task-5 lands.
"""

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
from secondsight.analysis.metrics import compute_segment_metrics
from secondsight.analysis.segmenter import Segmenter

__all__ = [
    "BehaviorFlag",
    "BehaviorFlagType",
    "Directive",
    "DirectiveStatus",
    "DirectiveType",
    "SegmentData",
    "SegmentMetrics",
    "Segmenter",
    "ToolUseSpan",
    "compute_segment_metrics",
]
