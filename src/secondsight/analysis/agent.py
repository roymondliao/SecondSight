"""AnalysisAgent — typed seam between GUR-102 (orchestrator/behavior/
aggregator) and GUR-103 (PydanticAI implementation).

Async-first because PydanticAI is async-native. Three explicit methods
(not a generic dispatcher) because each method has a fixed input/output
shape and GUR-103's implementation is most readable when each is a
separate function. Batched analyze_segments avoids Protocol churn when
GUR-103 adds concurrency (single-segment calls use len==1).

All methods raise AnalysisAgentError on irrecoverable failure (LLM
timeout exceeding policy, malformed JSON unrecoverable by the agent
layer, validation error). Caller (behavior.py / aggregator.py /
orchestrator.py) decides skip-segment vs. fail-pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from secondsight.analysis.prompts.aggregate import AggregateOutput
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import SegmentAnalysis


class AnalysisAgentError(Exception):
    """Irrecoverable agent-side failure. Caller decides skip vs. fail-loud."""


@runtime_checkable
class AnalysisAgent(Protocol):
    """Contract GUR-102 freezes; GUR-103 implements on PydanticAI.

    Bets ratified at planning gate (D1 in 2-plan.md):
    - Async-first: all methods return Awaitable. Sync wrappers are
      GUR-103's problem at the hook boundary, not this layer's.
    - All-or-nothing batch: analyze_segments has no partial-success
      semantics. Callers wanting per-prompt error isolation must call
      with len==1 in a loop.
    - Three explicit methods (not generic dispatch): chosen for
      type-clarity and GUR-103 implementation ergonomics.

    NOTE on @runtime_checkable: isinstance() checks method NAMES only —
    not signatures, parameter types, return types, or async-ness. A
    sync class with the right method names will pass isinstance() but
    fail at await-time. mypy / pyright catch the rest at type-check time.
    """

    async def analyze_segments(
        self,
        prompts: Sequence[str],
    ) -> list[SegmentAnalysis]:
        """Batched form. len(out) == len(in). Single-segment uses len==1.

        Validates each output against SegmentAnalysis. On any prompt's
        irrecoverable failure, raises AnalysisAgentError naming the
        prompt index. Partial success is NOT supported — all-or-nothing
        per call.
        """

    async def aggregate_flag_type(
        self,
        prompt: str,
    ) -> AggregateOutput:
        """One call per flag-type group. Aggregator does its own fan-out."""

    async def summarize_session(
        self,
        prompt: str,
    ) -> SummaryOutput:
        """One call per session."""
