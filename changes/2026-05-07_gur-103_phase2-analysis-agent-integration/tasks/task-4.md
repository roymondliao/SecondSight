# Task 4 (P2-12): `sdk/agent.py` — PydanticAIAnalysisAgent

## Context

Read: `overview.md`, `2-plan.md` §2 (D4), `2-pre-thinking.md` §C,
`src/secondsight/analysis/agent.py` (the frozen Protocol).

Implement the three-method `AnalysisAgent` Protocol on top of
PydanticAI. Each Protocol method gets its own PydanticAI `Agent`
instance with **scoped tool availability** so the aggregator
literally cannot reach `read_project_file` (D4 enforcement).

This task depends on task-1 (tools), task-2 (router), task-3
(model selection).

## Files

- Create: `src/secondsight/sdk/agent.py`
- Test: `tests/sdk/test_agent.py`

## Death Test Requirements

- **DT-4.1 aggregate_flag_type cannot reach read_project_file.**
  Construct a `PydanticAIAnalysisAgent` with `AnalysisTools`
  (real implementation from task-1). Use a FakeRouter that
  records every tool the underlying PydanticAI Agent attempts
  to invoke. Call `agent.aggregate_flag_type(prompt)`. Assert
  the FakeRouter saw a tool list NOT containing
  `read_project_file`. (Implementation detail: the test inspects
  the PydanticAI Agent's tool registry directly, OR the router
  fakes a model that "asks" to call `read_project_file` by
  emitting a tool-call message and asserts PydanticAI raises
  `UnknownToolName` or equivalent.)
- **DT-4.2 analyze_segments partial-batch failure.** Configure
  FakeRouter so the 2nd of 3 prompts raises
  `AnalysisAgentError("simulated")`. Call
  `agent.analyze_segments(["p1", "p2", "p3"])`. Assert raised
  `AnalysisAgentError` whose message contains `"prompt index 1"`
  (0-indexed). Assert no result was returned for `p3` (batch
  stops at first failure per Protocol contract).

## Implementation Steps

- [ ] Step 1: Write death tests (2 above).
- [ ] Step 2: Run — verify fail.
- [ ] Step 3: Write happy-path tests:
      - HP-1.2: `analyze_segments(prompts)` with FakeRouter →
        `len(result) == len(prompts)`; each is a Pydantic-validated
        `SegmentAnalysis`; FakeRouter.call invoked exactly
        `len(prompts)` times sequentially.
      - HP-4.3: single-segment `analyze_segments(["p1"])` golden
        path returns `[SegmentAnalysis(...)]`.
      - HP-extra: `aggregate_flag_type(prompt)` returns
        `AggregateOutput`; `summarize_session(prompt)` returns
        `SummaryOutput`.
- [ ] Step 4: Run — verify fail.
- [ ] Step 5: Implement:
      - `class PydanticAIAnalysisAgent` with constructor
        `(router: LLMRouter, tools: AnalysisTools)`.
      - Internal: three `pydantic_ai.Agent` instances, one per
        Protocol method, constructed at `__init__` time:
        - `_segment_agent: Agent[..., SegmentAnalysis]` with
          tools `[tools.read_traces, tools.read_project_file]`.
        - `_aggregate_agent: Agent[..., AggregateOutput]` with
          tools `[tools.read_historical_flags]`.
        - `_summary_agent: Agent[..., SummaryOutput]` with tools
          `[tools.read_traces, tools.query_structured_store]`.
      - `async def analyze_segments(self, prompts)`:
        - Sequential loop. For each `(i, p)`:
          - Try `await router.call(model_input=p,
            output_type=SegmentAnalysis)` (or via the
            `_segment_agent.run()` shim depending on PydanticAI
            integration shape — see scar items).
          - On exception: re-raise `AnalysisAgentError(f"batch
            failed at prompt index {i}: {exc}")` with
            `__cause__=exc`.
          - Append result to list.
        - Return list (length == len(prompts)).
      - `async def aggregate_flag_type(self, prompt)` →
        `await router.call(...)` once.
      - `async def summarize_session(self, prompt)` →
        `await router.call(...)` once.
      - Verify the class satisfies `analysis.agent.AnalysisAgent`
        Protocol via `isinstance` check at module-import time
        (assertion or self-test; the existing
        `tests/analysis/test_agent_protocol.py` from GUR-102 has
        the conformance pattern).
- [ ] Step 6: Run — verify pass.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- The integration shape between `LLMRouter.call()` and
  `pydantic_ai.Agent.run()` is the load-bearing question. Two
  options: (a) Router IS the layer that constructs PydanticAI
  Agents per call, (b) Agent constructs three PydanticAI Agents
  at init and Router is called inside the Agent's tool callbacks.
  Default (chosen): option (a) — Router takes a `model_input`
  and `output_type` and constructs the Agent internally,
  selecting the right model from the chain. The three "scoped
  Agents" in this class are conceptual; the actual tool-list
  enforcement happens in router.call by passing the tool list
  per call. Document either way; tests must assert the
  enforcement is per-method.
- Scoped tool availability per method is the security control
  enforcing D4. Tests in DT-4.1 must verify this works END TO
  END (LLM tries to call wrong tool → fails) — not just at the
  init/registration level.
- Sequential per-segment loop; concurrent batching is a v1 nice-
  to-have. Document this in the docstring.
- PydanticAI's structured output (`Agent.output_type=T`)
  enforces Pydantic validation; do NOT add a redundant
  `T.model_validate(result)` call after — it would mask which
  layer is doing validation.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DT-4.1, DT-4.2
- HP-1.2 (analyze_segments batch contract)
- HP-4.3 (single-segment golden path)
