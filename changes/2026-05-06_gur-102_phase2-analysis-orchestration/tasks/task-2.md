# Task 2 — `AnalysisAgent` Protocol + `AnalysisAgentError` + `FakeAnalysisAgent`

**Depends on:** none. **Blocks:** task-3, task-4, task-5.

## Goal

Freeze the typed contract that GUR-103 will implement on PydanticAI.
This task ships **no implementation** — only the Protocol, the
exception type, and a `FakeAnalysisAgent` test double that other tasks
can use.

## Files to create

- `src/secondsight/analysis/agent.py` — Protocol + exception
- `tests/analysis/test_agent_protocol.py` — Protocol contract + fake

## Files to modify

- `src/secondsight/analysis/__init__.py` — re-export `AnalysisAgent` and `AnalysisAgentError`

## Protocol body (verbatim — do not paraphrase)

```python
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
    """Contract GUR-102 freezes; GUR-103 implements on PydanticAI."""

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
```

## `FakeAnalysisAgent` test double

Lives in `tests/analysis/test_agent_protocol.py` (or an `_helpers`
module if multiple test files need it). The fake takes canned outputs
keyed by prompt content (or call index, depending on test ergonomics)
and returns them deterministically.

```python
class FakeAnalysisAgent:
    """Test double conforming to AnalysisAgent.

    Configure with canned outputs; calls assert deterministic behavior.
    Use raise_on_call_n=N to simulate an irrecoverable failure on the
    Nth call, which raises AnalysisAgentError.
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
    ) -> None: ...

    async def analyze_segments(
        self, prompts: Sequence[str]
    ) -> list[SegmentAnalysis]: ...

    async def aggregate_flag_type(self, prompt: str) -> AggregateOutput: ...

    async def summarize_session(self, prompt: str) -> SummaryOutput: ...
```

## Death tests (write FIRST)

- **DT-2.1 — Protocol cannot be instantiated.** Calling
  `AnalysisAgent()` raises `TypeError`. Documents that the Protocol
  is a contract, not a base class.
- **DT-2.2 — `isinstance` check works on conformant class.**
  `isinstance(FakeAnalysisAgent(...), AnalysisAgent)` is True;
  `isinstance(object(), AnalysisAgent)` is False. Verifies
  `@runtime_checkable` is correctly applied.
- **DT-2.3 — `analyze_segments` len contract.**
  `await fake.analyze_segments(["p1", "p2"])` returns exactly 2
  items. Calling with empty list returns empty list (not an error).
- **DT-2.4 — `raise_on_segments_call` raises `AnalysisAgentError`.**
  Configured fake raises on call; caller sees the typed exception.
- **DT-2.5 — Validation error on bad LLM output.** Constructing a
  fake with a `SegmentAnalysis` whose `flags[0].flag_type` is not in
  `BehaviorFlagType` enum: Pydantic raises at fake-construction time
  (catches the configuration mistake at test-setup, not at runtime).

## Happy-path tests

- **HP-2.1** — `FakeAnalysisAgent` round-trip: configure with
  segment_outputs of length 3, await `analyze_segments(["a","b","c"])`,
  receive the configured outputs in order.
- **HP-2.2** — Aggregate fake: `await fake.aggregate_flag_type("...")`
  returns the canned `AggregateOutput`.
- **HP-2.3** — Summary fake: `await fake.summarize_session("...")`
  returns the canned `SummaryOutput`.

## Scar items to record

- Three explicit methods (not a generic dispatch) — chosen for
  type-clarity and GUR-103 implementation ergonomics. Future
  alternatives (single `analyze[T](prompt: str) -> T`) are rejected
  because they require runtime type-tag plumbing.
- Async-first locks GUR-103 into PydanticAI's native async; sync
  wrappers (e.g., `asyncio.run` at the hook boundary) are GUR-103's
  problem to solve, not this layer's.
- `@runtime_checkable` enables `isinstance` but at the cost of only
  checking method *names* — not their signatures or async-ness.
  Mismatched implementations fail at call-time, not import-time.
  Acceptable trade-off (mypy catches the rest).
- `AnalysisAgentError` is a single exception class, not a hierarchy.
  If GUR-103 needs subtypes (e.g., `AnalysisAgentTimeoutError` vs
  `AnalysisAgentValidationError`) it can subclass without breaking
  callers that catch the base.
