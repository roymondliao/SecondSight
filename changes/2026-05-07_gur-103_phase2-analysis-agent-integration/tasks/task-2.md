# Task 2 (P2-13): `sdk/router.py` — LLMRouter

## Context

Read: `overview.md`, `2-plan.md` §3 (DC-3, DC-8), §4 (MH-2).

Wraps PydanticAI per-model agent calls with a transport-error-only
fallback chain. The cost-leak control: validation errors NEVER
trigger fallback. Closes DC-3 (validation error masquerading as
transport error) and DC-8 (empty fallback chain confusing error).

The router does NOT use LiteLLM as its routing layer — it uses
PydanticAI directly per provider, and we own the failure-mode
classification ourselves. LiteLLM is reserved as an *escape hatch*
for non-OpenAI-compatible providers, attached to PydanticAI as a
provider in `select_model` if needed.

## Files

- Create: `src/secondsight/sdk/__init__.py` (empty package init;
  this task introduces the `sdk/` directory)
- Create: `src/secondsight/sdk/router.py`
- Modify: `pyproject.toml` (add `pydantic-ai` dependency; pin to
  the latest stable version at planning time)
- Test: `tests/sdk/test_router.py`
- Test: `tests/sdk/__init__.py` (empty)

## Death Test Requirements

Write these BEFORE implementation:

- **DT-2.1 ValidationError in __cause__ chain → no fallback (DC-3).**
  Construct a fake primary that raises
  `pydantic_ai.exceptions.UnexpectedModelBehavior` whose
  `__cause__` is a `pydantic.ValidationError`. Configure router
  with two fallbacks. Call `router.call()`. Assert:
  - Raises `AnalysisAgentError` (not the original wrapped
    exception)
  - Fallback agents were called ZERO times (use mocks to assert
    call counts)
  - Log lines include the unwrapped ValidationError's message
- **DT-2.2 empty chain construction logs WARN; chain-exhaustion
  error message includes config (DC-8).** Construct router with
  `fallbacks=[]`. Assert WARN log captured. Configure primary to
  raise `httpx.TimeoutException`. Call `router.call()`. Assert
  raised `AnalysisAgentError` message contains
  "fallback_models is empty" and includes the chain trace
  (just the primary attempt).
- **DT-2.3 chain exhaustion raises with chain trace.** Primary
  + 2 fallbacks all raise `RateLimitError`. Assert raised
  `AnalysisAgentError` with `attempts` attribute listing all 3
  attempts, each with `(model_name, exception_class_name,
  duration_ms)`.
- **DT-2.4 chain_total_timeout enforcement.** Primary raises
  after a delay; fallbacks would succeed but cumulatively exceed
  the chain budget. Configure `chain_total_timeout_s=0.5`. Assert
  router raises `AnalysisAgentError` with reason
  "chain_total_timeout_exceeded" before all fallbacks attempted.

## Implementation Steps

- [ ] Step 1: Write death tests (4 above).
- [ ] Step 2: Run death tests — verify they fail.
- [ ] Step 3: Write degradation test (DG-1.1: fallback fires on
      RateLimitError; assert log has `outcome=fallback_triggered`)
      and happy-path test (HP-2.6: single primary success, zero
      fallback events).
- [ ] Step 4: Run all tests — verify they fail.
- [ ] Step 5: Implement:
      - `ModelSpec` dataclass: `(name: str, provider: str,
        api_key_env: str | None = None)`.
      - `LLMRouter` class:
        - `__init__(primary, fallbacks, per_call_timeout_s=60.0,
          chain_total_timeout_s=90.0)` — log WARN if
          `len(fallbacks) == 0`.
        - `async call(*, model_input, output_type)` — iterate
          `[primary] + fallbacks`. For each, construct a
          PydanticAI Agent with that ModelSpec; await
          `Agent.run(model_input, output_type=output_type)` with
          `asyncio.wait_for(per_call_timeout_s)`. On exception,
          call `_classify(exc)`:
          - If terminal (validation / non-fallback-eligible),
            wrap in `AnalysisAgentError` with chain trace and
            re-raise immediately.
          - If fallback-eligible, append to `attempts` list and
            continue to next model.
          - Track `chain_total_elapsed`; if exceeded, raise
            `AnalysisAgentError(reason="chain_total_timeout_
            exceeded")`.
        - On chain exhaustion: raise `AnalysisAgentError` with
          chain trace.
      - `_classify(exc)`:
        - Walk `exc.__cause__` AND `exc.__context__` chains
          (Python's two exception-chain mechanisms).
        - At any depth: `pydantic.ValidationError` → terminal.
        - At outermost: any of the allowlist transport errors →
          fallback-eligible.
        - `ProviderAuthError` → fallback-eligible only on the
          first encounter (track via a set of (provider,
          status_code) tuples).
        - Default: terminal (`unknown_error`).
      - Logging: every attempt logs `provider, model, tokens_in,
        tokens_out (best-effort), duration_ms, attempt,
        total_attempts, outcome` at INFO. The aggregator
        corruption-signature sub-metric depends on this format.
- [ ] Step 6: Run all tests — verify they pass.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- `_classify` walks both `__cause__` AND `__context__` chains —
  Python exception chaining has both mechanisms. Tests cover
  the `__cause__` case (DT-2.1); document the `__context__` case
  in the classifier comment.
- `ProviderAuthError` fallback-once-per-chain semantics rely on a
  per-call `_seen_auth_errors` set; verify it doesn't leak across
  calls (constructor-level vs. per-call scope).
- Per-call timeout (60s) and chain timeout (90s) are independent
  budgets — make sure both are enforced (a primary that takes
  85s leaves only 5s for the entire fallback chain).
- Logging schema (`provider, model, tokens_in, tokens_out,
  duration_ms, attempt, total_attempts, outcome`) is documented
  at module top — North Star sub-metric depends on this format
  staying stable.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DT-2.1 (DC-3), DT-2.2 (DC-8)
- DG-1.1 (degradation)
- HP-2.6 (single primary success)
