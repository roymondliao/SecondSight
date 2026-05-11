"""Shared FakeAnalysisAgent test double for task-3/4/5 tests (GUR-102).

Extracted from tests/analysis/test_agent_protocol.py to avoid circular
imports and allow task-3/4/5 to import without pulling in task-2's
test assertions.

Choice rationale (extract vs. copy):
  Extracted to a shared module. This prevents drift where two copies of
  FakeAnalysisAgent evolve differently. The original file in
  test_agent_protocol.py still imports from here so its tests continue
  to pass unmodified — see the import update at the bottom of that file.

  Alternative (inline copy) was rejected because DC-2 tests depend on
  model_construct bypassing Pydantic validation. If two copies drifted,
  one set of death tests could pass against a stale fake while the other
  fails silently.
"""

from __future__ import annotations

from collections.abc import Sequence

from secondsight.analysis.agent import AnalysisAgent, AnalysisAgentError
from secondsight.analysis.prompts.aggregate import AggregateOutput
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import SegmentAnalysis


class FakeAnalysisAgent:
    """Test double conforming to AnalysisAgent Protocol.

    Configure with canned outputs; calls assert deterministic behavior.

    segment_outputs:
        Returned by analyze_segments in order. len(out) == len(prompts)
        is enforced — if more prompts are passed than configured outputs,
        AnalysisAgentError is raised (not silent truncation). Empty prompts
        returns [].

    aggregate_outputs:
        Dict keyed by prompt string verbatim. Call with a prompt not in
        this dict raises KeyError — misconfigured fakes fail loudly.

    summary_output:
        Returned by every summarize_session call regardless of prompt text.
        If not configured and raise_on_summary_call is False, raises
        AnalysisAgentError (not RuntimeError) — fake misconfiguration
        surfaces with the same exception type the Protocol declares.

    raise_on_segments_call:
        If True, the next analyze_segments call raises AnalysisAgentError
        instead of returning canned outputs.

    raise_on_aggregate_call:
        If True, the next aggregate_flag_type call raises AnalysisAgentError.

    raise_on_summary_call:
        If True, the next summarize_session call raises AnalysisAgentError.
    """

    def __init__(
        self,
        segment_outputs: list[SegmentAnalysis] | None = None,
        aggregate_outputs: dict[str, AggregateOutput] | None = None,
        summary_output: SummaryOutput | None = None,
        *,
        raise_on_segments_call: bool = False,
        raise_on_aggregate_call: bool = False,
        raise_on_summary_call: bool = False,
    ) -> None:
        self._segment_outputs: list[SegmentAnalysis] = segment_outputs or []
        self._aggregate_outputs: dict[str, AggregateOutput] = aggregate_outputs or {}
        self._summary_output: SummaryOutput | None = summary_output
        self._raise_on_segments_call = raise_on_segments_call
        self._raise_on_aggregate_call = raise_on_aggregate_call
        self._raise_on_summary_call = raise_on_summary_call

    async def analyze_segments(
        self, prompts: Sequence[str]
    ) -> list[SegmentAnalysis]:
        """Return canned outputs aligned to prompts.

        Raises AnalysisAgentError if raise_on_segments_call is True.
        Raises AnalysisAgentError if len(prompts) > len(segment_outputs)
        — misconfigured fakes fail loudly with the Protocol's declared
        exception type (not silent truncation).
        Returns [] for empty prompts (not an error).
        """
        if self._raise_on_segments_call:
            raise AnalysisAgentError(
                "FakeAnalysisAgent: configured to raise on analyze_segments call"
            )
        if len(prompts) > len(self._segment_outputs):
            raise AnalysisAgentError(
                f"FakeAnalysisAgent misconfigured: {len(prompts)} prompts requested but "
                f"only {len(self._segment_outputs)} segment_outputs configured"
            )
        return list(self._segment_outputs[: len(prompts)])

    async def aggregate_flag_type(self, prompt: str) -> AggregateOutput:
        """Return canned AggregateOutput for the given prompt string.

        Raises AnalysisAgentError if raise_on_aggregate_call is True.
        Raises KeyError if prompt not in aggregate_outputs.
        """
        if self._raise_on_aggregate_call:
            raise AnalysisAgentError(
                "FakeAnalysisAgent: configured to raise on aggregate_flag_type call"
            )
        return self._aggregate_outputs[prompt]

    async def summarize_session(self, prompt: str) -> SummaryOutput:
        """Return the canned SummaryOutput regardless of prompt text.

        Raises AnalysisAgentError if raise_on_summary_call is True.
        Raises AnalysisAgentError if no summary_output was configured.
        """
        if self._raise_on_summary_call:
            raise AnalysisAgentError(
                "FakeAnalysisAgent: configured to raise on summarize_session call"
            )
        if self._summary_output is None:
            raise AnalysisAgentError(
                "FakeAnalysisAgent: summarize_session called but no summary_output configured. "
                "Pass summary_output=SummaryOutput(...) at construction."
            )
        return self._summary_output


__all__ = ["FakeAnalysisAgent"]
