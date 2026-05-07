"""Analysis layer — Phase 2 data contracts, read-side modules, and
prompt builders (GUR-100 + GUR-101).

Public surface:
- Pydantic schemas / enums (task-1): BehaviorFlag, BehaviorFlagDraft,
  BehaviorFlagType, Directive, DirectiveStatus, DirectiveType,
  FLAG_DEFINITIONS, FlagDefinition, SegmentAnalysis, SegmentData,
  SegmentMetrics, ToolUseSpan.
- Read-side assembly (task-4): Segmenter.
- Cross-session aggregator (task-4): aggregate_project_flags, compute_identity_key,
  AggregateProjectResult, DEFAULT_CONVENTION_TOP_N.
- Pure-function metrics (task-5): compute_segment_metrics.
- Orchestrator (task-5): Orchestrator, SessionIncompleteError,
  SessionAlreadyAnalyzedError, AnalyzeSessionResult, AnalyzeAndAggregateResult.
- Prompt builders (GUR-101 P2-5/6/7): see secondsight.analysis.prompts.
"""

from secondsight.analysis.agent import AnalysisAgent, AnalysisAgentError
from secondsight.analysis.aggregator import (
    DEFAULT_CONVENTION_TOP_N,
    AggregateProjectResult,
    aggregate_project_flags,
    compute_identity_key,
)
from secondsight.analysis.behavior import detect_segment_flags
from secondsight.analysis.metrics import compute_segment_metrics
from secondsight.analysis.orchestrator import (
    AnalyzeAndAggregateResult,
    AnalyzeSessionResult,
    Orchestrator,
    SessionAlreadyAnalyzedError,
    SessionIncompleteError,
)
from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    TERMINAL_STAGES,
    VALID_CONFIDENCE,
    AnalysisRun,
    AnalysisRunStage,
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
    SessionReport,
    ToolUseSpan,
)
from secondsight.analysis.segmenter import Segmenter

__all__ = [
    "AnalysisAgent",
    "AnalysisAgentError",
    "AnalyzeAndAggregateResult",
    "AnalyzeSessionResult",
    "AggregateProjectResult",
    "aggregate_project_flags",
    "compute_identity_key",
    "DEFAULT_CONVENTION_TOP_N",
    "detect_segment_flags",
    "FLAG_DEFINITIONS",
    "Orchestrator",
    "SessionAlreadyAnalyzedError",
    "SessionIncompleteError",
    "TERMINAL_STAGES",
    "VALID_CONFIDENCE",
    "AnalysisRun",
    "AnalysisRunStage",
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
    "SessionReport",
    "ToolUseSpan",
    "compute_segment_metrics",
]
