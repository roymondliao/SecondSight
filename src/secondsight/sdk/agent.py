"""PydanticAIAnalysisAgent — concrete AnalysisAgent Protocol implementation (GUR-103 P2-12).

Design: Path B (Agent owns three PydanticAI Agents with scoped tools).

Three internal LLMRouters are constructed at __init__ time, one per Protocol
method, each wired with a scoped agent_factory that enforces D4 per-method
tool scoping:
  - segment sub-router:   [read_traces, read_project_file]
  - aggregate sub-router: [read_historical_flags]
  - summary sub-router:   [read_traces, query_structured_store]

The aggregator literally cannot reach read_project_file because the PydanticAI
Agent created by its factory has read_project_file absent from its tool registry.
PydanticAI raises UnexpectedModelBehavior (unknown tool name) if the LLM requests
a tool not in the Agent's registry. This is a structural enforcement — not a policy
check at runtime.

Router integration:
  The caller-provided LLMRouter supplies the primary + fallback model chain and
  timeout configuration via its public ``config`` property (RouterConfig). The
  caller's router is NOT called directly — PydanticAIAnalysisAgent builds three
  internal sub-routers at __init__ time, one per method, each wired with the
  method-specific scoped agent_factory.

  For testing, pass ``agent_factory`` directly to PydanticAIAnalysisAgent. When
  provided, all three sub-routers use it (test double path). Tool scoping in the
  live path is verified via ``_scoped_tools`` (the callable lists each factory
  uses). The end-to-end DT-4.1 test verifies that PydanticAI's structural
  enforcement actually blocks forbidden tool calls.

Sequential batching:
  analyze_segments processes prompts sequentially (not concurrently) for v1.
  Concurrent batching is a v2 nice-to-have. The sequential contract is:
  - len(result) == len(prompts)
  - On failure at index i, the original exception (preserving its subclass type)
    gains a note naming the prompt index, then is re-raised as-is.
  - Prompts after index i are NOT attempted (all-or-nothing per Protocol)

Assumptions:
  1. LLMRouter.config returns a RouterConfig with the correct model chain and
     timeout values (verified by test_HP_router_config_public_api_returns_constructor_values
     in test_router.py).
  2. AnalysisTools methods (read_traces, read_project_file, etc.) have correct
     runtime-resolvable annotations (verified by removing TYPE_CHECKING guard from
     tools.py — GUR-103 fix-loop issue #6).
  3. pydantic-ai >= 1.87.0 supports tools= as a constructor parameter and
     tool scoping is enforced per-Agent instance (verified experimentally in
     2026-05-08 dev session).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.analysis.prompts.aggregate import AggregateOutput
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import SegmentAnalysis
from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.router import LLMRouter

if TYPE_CHECKING:
    from secondsight.analysis.tools import AnalysisTools

_logger = logging.getLogger(__name__)

# Type alias for a PydanticAI agent factory.
AgentFactory = Callable[[ModelSpec], Any]


def _spec_to_model_id(spec: ModelSpec) -> str:
    """Convert a ModelSpec to a PydanticAI model identifier string.

    Mirrors the convention in router._default_agent_factory:
    - OpenAI: model name only (no prefix)
    - Other providers: "{provider}:{name}"
    """
    if spec.provider == "openai":
        return spec.name
    return f"{spec.provider}:{spec.name}"


def _make_scoped_agent_factory(
    tools: list[Any],
    output_type: type,
) -> Callable[[ModelSpec], Agent]:  # type: ignore[type-arg]
    """Return an agent_factory that creates PydanticAI Agents with the given tool list.

    Each invocation creates a new Agent instance for the given model spec.
    The tool list enforces D4 per-method tool scoping: PydanticAI will raise
    UnexpectedModelBehavior if the LLM requests a tool not in this list.

    Args:
        tools: List of async callable tool functions registered with this agent.
        output_type: Pydantic model class for structured output validation.

    Returns:
        A callable ModelSpec -> Agent suitable for LLMRouter's agent_factory.
    """

    def factory(spec: ModelSpec) -> Agent:  # type: ignore[type-arg]
        model_id = _spec_to_model_id(spec)
        return Agent(
            model_id,
            output_type=output_type,
            tools=tools,
            defer_model_check=True,
        )

    return factory


class PydanticAIAnalysisAgent:
    """Concrete AnalysisAgent Protocol implementation using PydanticAI.

    Three Protocol methods, each with a dedicated internal LLMRouter and a
    dedicated scoped agent_factory (per-method tool scoping, D4).

    Construction:
        agent = PydanticAIAnalysisAgent(router=router, tools=tools)

    The provided ``router`` is used only to extract the model chain (primary,
    fallbacks) and timeout config via ``router.config``. Three internal
    sub-routers are created, each wired with the appropriate scoped agent_factory.
    The original ``router`` is NOT called directly.

    Test injection:
        agent = PydanticAIAnalysisAgent(router=router, tools=tools,
                                        agent_factory=my_test_double)
    When ``agent_factory`` is provided, all three sub-routers use it. This is
    the test double path — tool scoping is verified by inspecting ``_scoped_tools``
    (the DT-4.1 static check) rather than running a real LLM call.

    ``_scoped_tools`` attribute:
        A dict mapping method name → list of callable tool functions that the
        production scoped factories use. DT-4.1 inspects this to verify the
        live-path tool lists are correctly scoped without going through
        pydantic-ai's internal ``_function_toolset`` attribute.

    Raises:
        AnalysisAgentError: All Protocol methods raise this (or its subclasses)
            on irrecoverable failure. The subclass hierarchy is:
            AnalysisAgentError
              RouterChainExhaustedError
              RouterChainTimeoutError
              RouterTerminalError
    """

    def __init__(
        self,
        *,
        router: LLMRouter,
        tools: "AnalysisTools",
        agent_factory: AgentFactory | None = None,
    ) -> None:
        """Initialise the PydanticAI-backed analysis agent.

        Args:
            router: Pre-wired LLMRouter providing the model chain (primary +
                fallbacks) and timeout configuration via ``router.config``.
                The router's own agent_factory is NOT used.
            tools: AnalysisTools instance providing the four tool methods.
                Tool availability per method is enforced at this layer (D4):
                - analyze_segments:    [read_traces, read_project_file]
                - aggregate_flag_type: [read_historical_flags]
                - summarize_session:   [read_traces, query_structured_store]
            agent_factory: Optional test double factory. When provided, all
                three sub-routers use it instead of the scoped production
                factories. Intended for testing only. When None (default),
                three separate scoped factories are built — one per method —
                each creating PydanticAI Agents with the method-specific tool
                list (D4 structural enforcement).

        Design bet (D4, from 2-plan.md):
            Per-method tool scoping is a security control, not a hint. The
            aggregator CANNOT reach read_project_file because its sub-router's
            factory creates a PydanticAI Agent without read_project_file in its
            tool registry. If pydantic-ai's tool scoping mechanism changes across
            versions, the death test DT-4.1 end-to-end will catch it.
        """
        cfg = self._extract_router_config(router)
        self._scoped_tools = self._build_scoped_tools(tools)
        segment_router, aggregate_router, summary_router = self._build_sub_routers(
            primary=cfg.primary,
            fallbacks=cfg.fallbacks,
            per_call_timeout_s=cfg.per_call_timeout_s,
            chain_total_timeout_s=cfg.chain_total_timeout_s,
            scoped_tools=self._scoped_tools,
            override_factory=agent_factory,
        )
        self._segment_router = segment_router
        self._aggregate_router = aggregate_router
        self._summary_router = summary_router

        _logger.debug(
            f"PydanticAIAnalysisAgent constructed. "
            f"segment_tools={[fn.__name__ for fn in self._scoped_tools['segment']]!r} "
            f"aggregate_tools={[fn.__name__ for fn in self._scoped_tools['aggregate']]!r} "
            f"summary_tools={[fn.__name__ for fn in self._scoped_tools['summary']]!r} "
            f"primary={cfg.primary.name!r} fallback_count={len(cfg.fallbacks)} "
            f"override_factory={'yes' if agent_factory is not None else 'no'}"
        )

    # ------------------------------------------------------------------
    # Private construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_router_config(router: LLMRouter):  # type: ignore[return]
        """Extract the model chain and timeout config from the router's public API.

        Uses router.config (RouterConfig dataclass) — the public read API added
        in the GUR-103 fix-loop. No private attribute access.

        Death reason: if router.config returns wrong values, all three sub-routers
        will be configured with the wrong model chain or timeouts. The unit test
        test_HP_router_config_public_api_returns_constructor_values in test_router.py
        closes this path by verifying RouterConfig returns constructor values unchanged.

        Returns:
            RouterConfig with primary, fallbacks, per_call_timeout_s, chain_total_timeout_s.
        """
        return router.config

    @staticmethod
    def _build_scoped_tools(tools: "AnalysisTools") -> dict[str, list[Any]]:
        """Build the per-method tool lists for D4 scoping.

        Each method receives only the tools it is allowed to use. This dict
        is the authoritative source for which tools each sub-router's factory
        will pass to PydanticAI Agent construction.

        Death reason: if the wrong tool appears in a method's list, the LLM
        can reach data it shouldn't. DT-4.1 inspects this dict to catch
        misconfiguration at construction time.

        Returns:
            Dict with keys "segment", "aggregate", "summary". Values are lists
            of async callable tools.
        """
        return {
            "segment": [tools.read_traces, tools.read_project_file],
            "aggregate": [tools.read_historical_flags],
            "summary": [tools.read_traces, tools.query_structured_store],
        }

    @staticmethod
    def _build_sub_routers(
        *,
        primary: ModelSpec,
        fallbacks: list[ModelSpec],
        per_call_timeout_s: float,
        chain_total_timeout_s: float,
        scoped_tools: dict[str, list[Any]],
        override_factory: AgentFactory | None,
    ) -> tuple[LLMRouter, LLMRouter, LLMRouter]:
        """Construct three internal LLMRouters with method-scoped agent factories.

        Death reason: if all three use the same factory (wrong wiring), the
        aggregator gains access to read_project_file via the segment factory.
        DT-4.1 static check catches this via _scoped_tools; DT-4.1 end-to-end
        catches it if the live path passes a bad model.

        When override_factory is provided (test double path), all three
        sub-routers use it. Tool scoping is NOT enforced end-to-end in this
        path — DT-4.1 static check via _scoped_tools is the only verification.
        This is by design: test doubles cannot enforce PydanticAI's structural
        scoping without a real PydanticAI Agent.

        Returns:
            Tuple of (segment_router, aggregate_router, summary_router).
        """
        if override_factory is not None:
            # Test double path: caller provided an explicit factory.
            # Log a warning so it's visible if this path appears in production.
            _logger.warning(
                "PydanticAIAnalysisAgent: override agent_factory provided — "
                "using it for all three method sub-routers. End-to-end tool "
                "scoping (D4) is NOT enforced via PydanticAI for this path. "
                "This is expected in test contexts; verify if seen in production."
            )
            segment_factory: AgentFactory = override_factory
            aggregate_factory: AgentFactory = override_factory
            summary_factory: AgentFactory = override_factory
        else:
            # Production path: build three separate scoped factories.
            # Each factory creates a PydanticAI Agent with only the method's tools.
            segment_factory = _make_scoped_agent_factory(
                tools=scoped_tools["segment"],
                output_type=SegmentAnalysis,
            )
            aggregate_factory = _make_scoped_agent_factory(
                tools=scoped_tools["aggregate"],
                output_type=AggregateOutput,
            )
            summary_factory = _make_scoped_agent_factory(
                tools=scoped_tools["summary"],
                output_type=SummaryOutput,
            )

        common = dict(
            primary=primary,
            fallbacks=fallbacks,
            per_call_timeout_s=per_call_timeout_s,
            chain_total_timeout_s=chain_total_timeout_s,
        )
        return (
            LLMRouter(**common, agent_factory=segment_factory),
            LLMRouter(**common, agent_factory=aggregate_factory),
            LLMRouter(**common, agent_factory=summary_factory),
        )

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    async def analyze_segments(
        self,
        prompts: Sequence[str],
    ) -> list[SegmentAnalysis]:
        """Analyze each prompt sequentially, returning one SegmentAnalysis per prompt.

        Protocol contract:
          - len(result) == len(prompts)
          - Sequential processing: prompts are processed in order; no concurrency.
          - All-or-nothing: on any prompt's irrecoverable failure, the original
            exception (preserving its subclass type) gains a note naming the
            failing prompt index (0-indexed), then is re-raised as-is. Prompts
            after the failing index are NOT attempted.

        Args:
            prompts: Rendered prompt strings, one per segment.

        Returns:
            List of SegmentAnalysis, aligned with prompts by index.

        Raises:
            AnalysisAgentError (or subclass): On any prompt's irrecoverable failure.
                The exception's notes include "batch failed at prompt index {i}".
                The original subclass (RouterChainExhaustedError, RouterTerminalError,
                etc.) is preserved so callers can distinguish failure modes.
        """
        results: list[SegmentAnalysis] = []
        for i, prompt in enumerate(prompts):
            try:
                result = await self._segment_router.call(
                    model_input=prompt,
                    output_type=SegmentAnalysis,
                )
                results.append(result)
            except AnalysisAgentError as exc:
                _logger.warning(f"analyze_segments: batch failed at prompt index {i}: {exc}")
                # Preserve the original exception type and its __cause__ chain.
                # add_note() (Python 3.11+) attaches context without wrapping.
                # This lets callers distinguish RouterChainExhaustedError from
                # RouterTerminalError etc. — wrapping in base AnalysisAgentError
                # would lose this information.
                exc.add_note(f"batch failed at prompt index {i}")
                raise
        return results

    async def aggregate_flag_type(self, prompt: str) -> AggregateOutput:
        """Aggregate flags for one flag-type group into convention patterns.

        Tool scope: [read_historical_flags] only. read_project_file is NOT
        available to this method's PydanticAI Agent (D4 enforcement).

        Args:
            prompt: Rendered aggregation prompt for one flag type.

        Returns:
            AggregateOutput with discovered patterns and conventions.

        Raises:
            AnalysisAgentError: On irrecoverable failure.
        """
        return await self._aggregate_router.call(
            model_input=prompt,
            output_type=AggregateOutput,
        )

    async def summarize_session(self, prompt: str) -> SummaryOutput:
        """Summarize one session's analysis into a dashboard-ready report.

        Tool scope: [read_traces, query_structured_store]. read_project_file
        is NOT available to this method's PydanticAI Agent (D4 enforcement).

        Args:
            prompt: Rendered summary prompt for one session.

        Returns:
            SummaryOutput with headline, key_findings, and body.

        Raises:
            AnalysisAgentError: On irrecoverable failure.
        """
        return await self._summary_router.call(
            model_input=prompt,
            output_type=SummaryOutput,
        )


__all__ = ["PydanticAIAnalysisAgent"]
