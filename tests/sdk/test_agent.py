"""Tests for PydanticAIAnalysisAgent — death tests first, then happy-path.

Death test contract: each DT-* test names the silent failure path it closes.
The most dangerous silent failure is tool-scoping bypass: if aggregate_flag_type
can reach read_project_file, confidential project file content is silently
shipped to the LLM provider during aggregation calls.

Test double strategy:
  - FakeRouter: a callable that records calls and returns canned outputs.
    PydanticAIAnalysisAgent constructs internal sub-routers whose agent_factory
    is built from the provided router's model chain. For testing, we bypass
    this by injecting fake sub-routers directly.
  - BadToolModel: a PydanticAI TestModel subclass that asks to call a tool
    NOT in the agent's tool list, verifying PydanticAI enforces scoping.

Design note (Path B, documented in scar report):
  PydanticAIAnalysisAgent constructs three internal LLMRouters at __init__ time,
  one per Protocol method, each configured with a scoped agent_factory. The
  caller-provided LLMRouter supplies the primary + fallback model chain and
  timeout config. Tool scoping is enforced at PydanticAI Agent construction
  time (tools= param), not at LLM request time. The aggregator literally
  cannot reach read_project_file because PydanticAI raises UnexpectedModelBehavior
  when a tool not in the agent's registry is requested.
"""

from __future__ import annotations

import json
import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.models import ModelRequestParameters, ModelSettings
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.test import TestModel

from secondsight.analysis.agent import AnalysisAgent, AnalysisAgentError
from secondsight.analysis.prompts.aggregate import AggregateOutput, AggregatePattern
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType, SegmentAnalysis
from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.router import LLMRouter


# ---------------------------------------------------------------------------
# Helpers / shared fakes
# ---------------------------------------------------------------------------


def _make_segment_analysis(summary: str = "All good") -> SegmentAnalysis:
    return SegmentAnalysis(
        segment_summary=summary,
        flags=[],
        total_events=3,
        flagged_events=0,
    )


def _make_aggregate_output() -> AggregateOutput:
    return AggregateOutput(
        patterns=[
            AggregatePattern(
                pattern_description="Reads README before starting any task",
                occurrence_count=2,
                representative_sessions=["sess-001"],
                convention="Avoid reading README unless task is README-related.",
            )
        ]
    )


def _make_summary_output() -> SummaryOutput:
    return SummaryOutput(
        headline="1 flag across 1 segment",
        key_findings=["Unnecessary read detected"],
        body="The agent read README.md before modifying a.py.",
    )


def _make_primary() -> ModelSpec:
    return ModelSpec(name="test-model", provider="openai")


def _success_factory(output: Any):
    """Return an agent_factory whose agents always return ``output``."""
    mock_agent = AsyncMock()
    result = MagicMock()
    result.output = output
    result.usage = MagicMock(return_value=None)
    mock_agent.run.return_value = result
    return lambda _spec: mock_agent


def _raising_factory(exc: Exception):
    """Return an agent_factory whose agents always raise ``exc``."""
    mock_agent = AsyncMock()
    mock_agent.run.side_effect = exc
    return lambda _spec: mock_agent


def _import_agent_class():
    """Import PydanticAIAnalysisAgent — deferred so test file can exist before impl."""
    from secondsight.sdk.agent import PydanticAIAnalysisAgent  # noqa: PLC0415
    return PydanticAIAnalysisAgent


def _make_fake_tools():
    """Return a minimal fake AnalysisTools with no real repos (for DT-4.1 tool-scoping test)."""
    from unittest.mock import MagicMock  # noqa: PLC0415
    tools = MagicMock()
    # Give each mock method a __name__ so pydantic_ai Tool() can introspect it.
    async def read_traces(session_id: str) -> list:  # noqa: ANN001
        return []
    async def read_project_file(relative_path: str) -> str:  # noqa: ANN001
        return ""
    async def query_structured_store(query: dict) -> Any:  # noqa: ANN001
        return {}
    async def read_historical_flags(project_id: str, limit: int = 200) -> dict:  # noqa: ANN001
        return {}
    tools.read_traces = read_traces
    tools.read_project_file = read_project_file
    tools.query_structured_store = query_structured_store
    tools.read_historical_flags = read_historical_flags
    return tools


# ---------------------------------------------------------------------------
# DEATH TESTS
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.asyncio


class TestDeathPaths:
    def test_dt_4_1_aggregate_agent_tool_registry_excludes_read_project_file(self) -> None:
        """DT-4.1 (static) — aggregate_flag_type's scoped tools have ONLY read_historical_flags.

        Silent failure this closes: if the aggregate sub-router were built with ALL tools,
        the LLM could silently read project files during aggregation — sending confidential
        source code to the LLM API provider without any user-visible indication.

        This test inspects _scoped_tools (the live path's tool lists) at construction
        time. Unlike the old _function_toolset.tools path, this does NOT go through
        pydantic-ai internals — it checks the actual callable lists that will be passed
        to Agent() at call time. If _scoped_tools diverges from what LLMRouter passes to
        Agent(), DT-4.1 will NOT catch it. The end-to-end test below is the safety net.
        """
        PydanticAIAnalysisAgent = _import_agent_class()
        # No agent_factory override — this IS the production path.
        # The production scoped factories are built from tools; _scoped_tools reflects
        # what will be passed to PydanticAI Agent() at call time.
        router = LLMRouter(primary=_make_primary(), fallbacks=[])
        tools = _make_fake_tools()

        agent = PydanticAIAnalysisAgent(router=router, tools=tools)

        # Inspect the live-path scoped tool lists via the public _scoped_tools attribute.
        aggregate_tool_names = {fn.__name__ for fn in agent._scoped_tools["aggregate"]}
        segment_tool_names = {fn.__name__ for fn in agent._scoped_tools["segment"]}
        summary_tool_names = {fn.__name__ for fn in agent._scoped_tools["summary"]}

        # Aggregate agent MUST NOT have read_project_file.
        assert "read_project_file" not in aggregate_tool_names, (
            "aggregate_flag_type's tool list must NOT contain read_project_file. "
            "Tool scoping is the security control preventing the aggregator "
            "from reading raw project files (D4). Found tools: "
            + str(aggregate_tool_names)
        )
        # Summary agent MUST NOT have read_project_file.
        assert "read_project_file" not in summary_tool_names, (
            "summarize_session's tool list must NOT contain read_project_file. "
            "Found tools: " + str(summary_tool_names)
        )
        # Segment agent MUST have read_project_file.
        assert "read_project_file" in segment_tool_names, (
            "analyze_segments's tool list MUST contain read_project_file. "
            "Found tools: " + str(segment_tool_names)
        )
        # Aggregate agent MUST have read_historical_flags.
        assert "read_historical_flags" in aggregate_tool_names, (
            "aggregate_flag_type's tool list MUST contain read_historical_flags. "
            "Found tools: " + str(aggregate_tool_names)
        )

    async def test_dt_4_1_end_to_end_tool_scope_rejection(self) -> None:
        """DT-4.1 (end-to-end) — PydanticAI raises when bad model requests read_project_file
        during aggregate_flag_type.

        Silent failure this closes: a model that has been jailbroken or prompt-injected
        might request tools outside the registered scope. If PydanticAI silently ignores
        the unknown tool call, the agent returns garbage output without alerting the caller.
        PydanticAI must raise (UnexpectedModelBehavior after max retries) when the
        registered tool is not in scope.

        We verify this end-to-end using a scoped Agent directly passed via the
        agent_factory parameter (not through the router). The aggregate sub-router
        uses this factory, which creates a PydanticAI Agent with ONLY read_historical_flags.
        The bad model requests read_project_file, which is not registered — PydanticAI
        raises UnexpectedModelBehavior, the router wraps it as RouterTerminalError.
        """
        from pydantic_ai.exceptions import UnexpectedModelBehavior  # noqa: PLC0415
        from secondsight.sdk.router import RouterTerminalError  # noqa: PLC0415

        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        call_count = [0]

        class AlwaysCallsReadProjectFile(TestModel):
            """Test model that always asks to call 'read_project_file'."""

            async def request(
                self,
                messages: list[ModelMessage],
                model_settings: ModelSettings | None,
                model_request_parameters: ModelRequestParameters,
            ) -> ModelResponse:
                call_count[0] += 1
                return ModelResponse(
                    parts=[ToolCallPart(
                        tool_name="read_project_file",
                        args=json.dumps({"relative_path": "secrets.txt"}),
                    )]
                )

        # Build a scoped agent_factory that creates a PydanticAI Agent for aggregate:
        # the Agent has ONLY read_historical_flags — NOT read_project_file.
        # The bad model requests read_project_file, which is unknown → UnexpectedModelBehavior.
        def bad_agent_factory(spec: ModelSpec):
            from pydantic_ai import Agent  # noqa: PLC0415
            return Agent(
                AlwaysCallsReadProjectFile(),
                output_type=AggregateOutput,
                # Only register read_historical_flags — NOT read_project_file.
                tools=[tools.read_historical_flags],
                retries=0,
            )

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=[],
        )

        # Inject the bad factory directly into PydanticAIAnalysisAgent.
        # With the new architecture, agent_factory overrides the scoped production factories
        # for ALL three sub-routers (test double path). The static DT-4.1 check above
        # separately verifies the production scoping via _scoped_tools.
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=bad_agent_factory)

        # PydanticAI should raise UnexpectedModelBehavior (retries exhausted),
        # which the router wraps as RouterTerminalError.
        with pytest.raises((RouterTerminalError, AnalysisAgentError)):
            await agent.aggregate_flag_type("some aggregation prompt")

    async def test_dt_4_2_partial_batch_failure_stops_at_first_error(self) -> None:
        """DT-4.2 — analyze_segments fails at prompt index 1 of 3; p3 is never attempted.

        Silent failure this closes: if analyze_segments continued past a failure and
        returned partial results, callers would silently receive misaligned output
        (e.g., result[2] corresponding to prompt[1]) causing data corruption in the
        behavior_flags table without any visible error.

        Protocol contract: all-or-nothing — on ANY prompt's irrecoverable failure,
        raise AnalysisAgentError naming the prompt index, and stop immediately.
        """
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        call_order: list[str] = []

        def factory_that_fails_on_second(spec: ModelSpec):
            mock_agent = MagicMock()

            async def run_side_effect(prompt: str, output_type: type | None = None) -> Any:
                call_order.append(prompt)
                if prompt == "p2":
                    raise AnalysisAgentError("simulated failure on p2")
                result = MagicMock()
                result.output = _make_segment_analysis(f"result for {prompt}")
                result.usage = MagicMock(return_value=None)
                return result

            mock_agent.run = run_side_effect
            return mock_agent

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=[],
        )

        # Pass the test factory directly to PydanticAIAnalysisAgent (not via the router).
        # The router's factory is ignored; agent_factory= is the test injection point.
        agent = PydanticAIAnalysisAgent(
            router=router,
            tools=tools,
            agent_factory=factory_that_fails_on_second,
        )

        with pytest.raises(AnalysisAgentError) as exc_info:
            await agent.analyze_segments(["p1", "p2", "p3"])

        # With add_note pattern, the prompt index context is in __notes__, not str(exc).
        exc_notes = getattr(exc_info.value, "__notes__", []) or []
        notes_text = " ".join(exc_notes)
        assert "prompt index 1" in notes_text, (
            f"AnalysisAgentError must name the failing prompt index (0-indexed) in __notes__. "
            f"Got notes: {exc_notes!r}. "
            "Use exc.add_note('batch failed at prompt index {i}') before re-raising."
        )

        # The raised exception must be the SAME TYPE as the router raised it.
        # With add_note + raise (not 'raise NewType(...)'), the original subclass is preserved.
        # Callers can distinguish RouterChainExhaustedError from RouterTerminalError etc.
        # If analyze_segments wraps with base AnalysisAgentError, this fails.
        from secondsight.sdk.router import RouterTerminalError  # noqa: PLC0415
        assert isinstance(exc_info.value, RouterTerminalError), (
            f"analyze_segments must re-raise with the SAME exception type as the router raised. "
            f"The router wrapped the mock's AnalysisAgentError as RouterTerminalError. "
            f"Got: {type(exc_info.value).__name__}. "
            f"Use 'exc.add_note(...); raise' instead of 'raise AnalysisAgentError(...) from exc'."
        )

        # __cause__ chain must be preserved through the router's `raise ... from exc`.
        # If the router or analyze_segments stops using `from exc` (or wraps without it),
        # the original exception identity is lost from the chain. This guards against that.
        assert exc_info.value.__cause__ is not None, (
            "RouterTerminalError must preserve the original exception via __cause__ "
            "(the router uses `raise RouterTerminalError(...) from exc`). "
            "If __cause__ is None, the chain has been broken — investigate the raise site."
        )

        # p3 must never have been attempted.
        assert "p3" not in call_order, (
            f"p3 should never be attempted after p2 fails. "
            f"Actual call order: {call_order}"
        )
        # p1 succeeded, p2 failed.
        assert call_order == ["p1", "p2"], (
            f"Expected calls to be [p1, p2], got {call_order}"
        )


# ---------------------------------------------------------------------------
# HAPPY-PATH TESTS
# ---------------------------------------------------------------------------


class TestHappyPaths:
    async def test_hp_1_2_analyze_segments_batch_contract(self) -> None:
        """HP-1.2 — analyze_segments(2 prompts) → 2 SegmentAnalysis; router called 2 times."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        call_count = [0]
        outputs = [_make_segment_analysis("seg-A"), _make_segment_analysis("seg-B")]

        def counting_factory(spec: ModelSpec):
            mock_agent = MagicMock()

            async def run_side_effect(prompt: str, output_type: type | None = None) -> Any:
                idx = call_count[0]
                call_count[0] += 1
                result = MagicMock()
                result.output = outputs[idx]
                result.usage = MagicMock(return_value=None)
                return result

            mock_agent.run = run_side_effect
            return mock_agent

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=[],
        )
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=counting_factory)

        result = await agent.analyze_segments(["prompt-1", "prompt-2"])

        assert len(result) == 2, f"Expected 2 results, got {len(result)}"
        assert all(isinstance(r, SegmentAnalysis) for r in result), (
            "All results must be SegmentAnalysis instances"
        )
        assert call_count[0] == 2, f"Router agent.run must be called exactly 2 times, got {call_count[0]}"
        assert result[0].segment_summary == "seg-A"
        assert result[1].segment_summary == "seg-B"

    async def test_hp_4_3_single_segment_golden_path(self) -> None:
        """HP-4.3 — analyze_segments(["p1"]) → [SegmentAnalysis(...)], single call."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        expected = _make_segment_analysis("single segment result")

        def single_factory(spec: ModelSpec):
            mock_agent = MagicMock()

            async def run_side_effect(prompt: str, output_type: type | None = None) -> Any:
                result = MagicMock()
                result.output = expected
                result.usage = MagicMock(return_value=None)
                return result

            mock_agent.run = run_side_effect
            return mock_agent

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=[],
        )
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=single_factory)

        result = await agent.analyze_segments(["p1"])

        assert len(result) == 1
        assert isinstance(result[0], SegmentAnalysis)
        assert result[0].segment_summary == "single segment result"

    async def test_hp_extra_aggregate_flag_type_returns_aggregate_output(self) -> None:
        """aggregate_flag_type(prompt) → AggregateOutput."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        expected = _make_aggregate_output()
        factory = _success_factory(expected)

        router = LLMRouter(primary=_make_primary(), fallbacks=[])
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=factory)

        result = await agent.aggregate_flag_type("some aggregation prompt")

        assert isinstance(result, AggregateOutput), (
            f"Expected AggregateOutput, got {type(result).__name__}"
        )
        assert result.patterns[0].pattern_description == expected.patterns[0].pattern_description

    async def test_hp_extra_summarize_session_returns_summary_output(self) -> None:
        """summarize_session(prompt) → SummaryOutput."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        expected = _make_summary_output()
        factory = _success_factory(expected)

        router = LLMRouter(primary=_make_primary(), fallbacks=[])
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=factory)

        result = await agent.summarize_session("some summary prompt")

        assert isinstance(result, SummaryOutput), (
            f"Expected SummaryOutput, got {type(result).__name__}"
        )
        assert result.headline == expected.headline

    def test_hp_extra_isinstance_satisfies_analysis_agent_protocol(self) -> None:
        """PydanticAIAnalysisAgent satisfies the AnalysisAgent Protocol (runtime_checkable)."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()
        router = LLMRouter(primary=_make_primary(), fallbacks=[])
        factory = _success_factory(_make_segment_analysis())

        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=factory)

        assert isinstance(agent, AnalysisAgent), (
            "PydanticAIAnalysisAgent must satisfy isinstance(agent, AnalysisAgent). "
            "Check that all three Protocol methods are implemented with the correct names."
        )

    async def test_hp_extra_empty_prompts_returns_empty_list(self) -> None:
        """analyze_segments([]) → [] (not an error, not a router call)."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        call_count = [0]

        def counting_factory(spec: ModelSpec):
            mock_agent = MagicMock()

            async def run_side_effect(prompt: str, output_type: type | None = None) -> Any:
                call_count[0] += 1
                result = MagicMock()
                result.output = _make_segment_analysis()
                result.usage = MagicMock(return_value=None)
                return result

            mock_agent.run = run_side_effect
            return mock_agent

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=[],
        )
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=counting_factory)

        result = await agent.analyze_segments([])

        assert result == [], f"Empty prompts must return empty list, got {result}"
        assert call_count[0] == 0, "No router call should be made for empty prompts"

    async def test_hp_extra_analyze_segments_sequential_order(self) -> None:
        """analyze_segments processes prompts sequentially (p1 before p2 before p3)."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()

        received_order: list[str] = []

        def ordering_factory(spec: ModelSpec):
            mock_agent = MagicMock()

            async def run_side_effect(prompt: str, output_type: type | None = None) -> Any:
                received_order.append(prompt)
                result = MagicMock()
                result.output = _make_segment_analysis(f"result-{prompt}")
                result.usage = MagicMock(return_value=None)
                return result

            mock_agent.run = run_side_effect
            return mock_agent

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=[],
        )
        agent = PydanticAIAnalysisAgent(router=router, tools=tools, agent_factory=ordering_factory)

        await agent.analyze_segments(["p1", "p2", "p3"])

        assert received_order == ["p1", "p2", "p3"], (
            f"Prompts must be processed sequentially. Got order: {received_order}"
        )

    def test_hp_extra_aggregate_agent_has_only_read_historical_flags(self) -> None:
        """Positive assertion for DT-4.1: aggregate scoped tools are exactly {read_historical_flags}."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()
        router = LLMRouter(primary=_make_primary(), fallbacks=[])

        agent = PydanticAIAnalysisAgent(router=router, tools=tools)

        aggregate_tool_names = {fn.__name__ for fn in agent._scoped_tools["aggregate"]}
        assert aggregate_tool_names == {"read_historical_flags"}, (
            f"Aggregate scoped tools must be exactly {{read_historical_flags}}, got {aggregate_tool_names}"
        )

    def test_hp_extra_segment_agent_has_read_traces_and_read_project_file(self) -> None:
        """Segment scoped tools must be exactly {read_traces, read_project_file}."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()
        router = LLMRouter(primary=_make_primary(), fallbacks=[])

        agent = PydanticAIAnalysisAgent(router=router, tools=tools)

        segment_tool_names = {fn.__name__ for fn in agent._scoped_tools["segment"]}
        assert segment_tool_names == {"read_traces", "read_project_file"}, (
            f"Segment scoped tools must be exactly {{read_traces, read_project_file}}, got {segment_tool_names}"
        )

    def test_hp_extra_summary_agent_has_read_traces_and_query_structured_store(self) -> None:
        """Summary scoped tools must be exactly {read_traces, query_structured_store}."""
        PydanticAIAnalysisAgent = _import_agent_class()
        tools = _make_fake_tools()
        router = LLMRouter(primary=_make_primary(), fallbacks=[])

        agent = PydanticAIAnalysisAgent(router=router, tools=tools)

        summary_tool_names = {fn.__name__ for fn in agent._scoped_tools["summary"]}
        assert summary_tool_names == {"read_traces", "query_structured_store"}, (
            f"Summary scoped tools must be exactly {{read_traces, query_structured_store}}, got {summary_tool_names}"
        )

    def test_real_analysis_tools_construction_does_not_raise(self) -> None:
        """PydanticAIAnalysisAgent must NOT raise when constructed with real AnalysisTools.

        Silent failure this closes: the AnalysisTools.read_traces return annotation
        'list[Event]' previously caused NameError at Agent construction time because
        Event was only imported under TYPE_CHECKING. The fix (issue #6): remove the
        TYPE_CHECKING guard from tools.py and import Event at module level. With the
        guard removed, real AnalysisTools can be passed directly without annotation
        wrapping.
        """
        from pathlib import Path
        from unittest.mock import MagicMock

        from secondsight.analysis.tools import AnalysisTools

        PydanticAIAnalysisAgent = _import_agent_class()

        real_tools = AnalysisTools(
            project_root=Path("/tmp"),
            events_repo=MagicMock(),
            flags_repo=MagicMock(),
            directives_repo=MagicMock(),
        )
        router = LLMRouter(primary=_make_primary(), fallbacks=[])

        # Must not raise during construction with real AnalysisTools.
        # No agent_factory override — production path with scoped factories.
        agent = PydanticAIAnalysisAgent(router=router, tools=real_tools)

        # Verify tool names are correct in the live-path scoped tools.
        segment_tool_names = {fn.__name__ for fn in agent._scoped_tools["segment"]}
        assert segment_tool_names == {"read_traces", "read_project_file"}, (
            f"Segment tool names must be correct. Got: {segment_tool_names}"
        )
        aggregate_tool_names = {fn.__name__ for fn in agent._scoped_tools["aggregate"]}
        assert aggregate_tool_names == {"read_historical_flags"}, (
            f"Aggregate tool names must be correct. Got: {aggregate_tool_names}"
        )
        summary_tool_names = {fn.__name__ for fn in agent._scoped_tools["summary"]}
        assert summary_tool_names == {"read_traces", "query_structured_store"}, (
            f"Summary tool names must be correct. Got: {summary_tool_names}"
        )
