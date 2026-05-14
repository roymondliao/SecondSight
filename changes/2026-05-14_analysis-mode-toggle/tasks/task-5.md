# Task 5: SDK mode key injection fix (explicit AnthropicProvider(api_key=...))

## Context

Read: `overview.md`, `2-plan.md` §4 and DC7/DC8.

This task fixes the production silent failure: `RouterTerminalError: UserError: Set the ANTHROPIC_API_KEY environment variable or pass it via AnthropicProvider(api_key=...)`. Root cause: pydantic-ai's `AnthropicProvider` reads `os.environ["ANTHROPIC_API_KEY"]` implicitly when no key is passed; the current `LLMRouter` constructs `Agent(model_id)` without explicit provider injection.

Fix per Decision E1: loader resolves `${VAR}` interpolation ONCE at config load time and passes resolved keys (`resolved_keys: dict[str, str]`) into `LLMRouter.__init__`. The router constructs providers with `AnthropicProvider(api_key=resolved_keys["anthropic"])` explicitly. No implicit env fallback anywhere.

Mid-flight env mutation has NO effect (DC8): the resolved key is cached at load time. Documented behavior. Key rotation requires server restart.

This task also wraps the existing PydanticAI dispatch path into a thin `SDKAnalysisDispatcher` class for Task 6 to call uniformly.

## Files

- Create: `src/secondsight/analysis/sdk_dispatcher.py`
- Modify: `src/secondsight/sdk/router.py` — `LLMRouter.__init__` accepts `resolved_keys: dict[str, str]`; constructs providers explicitly; raises `RouterTerminalError` at __init__ if no provider can be constructed (vs. at first request)
- Modify: `src/secondsight/sdk/agent.py` — `PydanticAIAnalysisAgent` factory accepts pre-built `Agent` (with explicit provider) instead of constructing it from model_id alone
- Modify: `src/secondsight/config/loader.py` — at config load completion, call a helper `_resolve_provider_keys(providers: ProvidersConfig) -> dict[str, str]` that materializes `{"anthropic": "<resolved>", "openai": "<resolved>", "custom": "<resolved>"}` and attaches it to `SecondSightConfig` (e.g., new field `resolved_provider_keys`)
- Test: `tests/sdk/test_router_key_injection.py`
- Test: `tests/sdk/test_router_no_implicit_env.py` — DC7 verification
- Test: `tests/sdk/test_router_cache_once.py` — DC8 verification
- Test: `tests/analysis/test_sdk_dispatcher.py`

## Death Test Requirements

Before any implementation:

- Test (DC7): config has `[providers.anthropic] ANTHROPIC_API_KEY = ""` AND env `$ANTHROPIC_API_KEY="sk-from-env"` → loader's resolved_keys["anthropic"] equals `""` (NOT "sk-from-env"); LLMRouter init raises `RouterTerminalError` mentioning "no provider keys resolvable"
- Test: config has `ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"` AND env `$ANTHROPIC_API_KEY="sk-from-env"` → loader's resolved_keys["anthropic"] equals "sk-from-env"; LLMRouter init succeeds; pydantic-ai's `AnthropicProvider` is constructed with `api_key="sk-from-env"` explicit (assert by inspecting the constructed provider instance)
- Test (DC8): config resolved with env `$ANTHROPIC_API_KEY="sk-A"`; subsequently env changed to `$ANTHROPIC_API_KEY="sk-B"`; dispatch fires → request still uses sk-A (assert by intercepting the actual HTTP request or via a mock provider)
- Test: ALL three provider sections empty → `RouterTerminalError` at LLMRouter init (not at first request)
- Test: pydantic-ai's Agent is NOT constructed via `Agent(model_id_string)` shortcut anywhere — assert by searching the source for `Agent(` calls that don't pass an explicit `model=` with provider kwarg
- Test: when fallback_model is set and primary fails, fallback_used flag in returned AnalysisOutput is True; when fallback also fails, error_details contains BOTH primary AND fallback error info

## Implementation Steps

- [ ] Step 1: Write death tests
- [ ] Step 2: Run death tests — verify failure
- [ ] Step 3: Update `LLMRouter.__init__` signature to accept `resolved_keys: dict[str, str]`
- [ ] Step 4: Inside `LLMRouter.__init__`, construct providers explicitly:
  ```python
  providers = {}
  if resolved_keys.get("anthropic"):
      providers["anthropic"] = AnthropicProvider(api_key=resolved_keys["anthropic"])
  if resolved_keys.get("openai"):
      providers["openai"] = OpenAIProvider(api_key=resolved_keys["openai"])
  # custom: OpenAIProvider with base_url override
  if not providers:
      raise RouterTerminalError("no provider keys resolvable; mode=sdk requires at least one")
  ```
- [ ] Step 5: Update `PydanticAIAnalysisAgent` to accept a pre-built `Agent` (not construct it from model_id alone). The router now owns provider construction.
- [ ] Step 6: Update `src/secondsight/config/loader.py` to materialize `resolved_provider_keys` at config load. Add the field to `SecondSightConfig` dataclass (or via a side-channel; if dataclass is frozen, this may need to be a returned tuple `(config, resolved_keys)`)
- [ ] Step 7: Implement `src/secondsight/analysis/sdk_dispatcher.py`:
  - `class SDKAnalysisDispatcher`
  - `__init__(config: AnalysisConfig, resolved_keys: dict[str, str], prompt_loader)`
  - `async dispatch(session_id, session_payload) -> AnalysisOutput` — uses existing PydanticAI Agent.run() flow but wraps result into `AnalysisOutput` with `dispatched_via="sdk"`, `primary_model=...`, `fallback_used=...`
- [ ] Step 8: Run all tests
- [ ] Step 9: Run `pre-commit run --all-files`
- [ ] Step 10: Write scar report
- [ ] Step 11: Commit

## Expected Scar Report Items

- Potential shortcut: leaving the old `Agent(model_id)` path as a "fallback if no resolved_keys passed." **Don't.** That's literally the bug; preserving the path = preserving the bug.
- Potential shortcut: `os.environ["ANTHROPIC_API_KEY"] = resolved_keys["anthropic"]` (write env from config). **Don't.** That makes the cache-once contract weaker (env mutation visible elsewhere) and re-introduces implicit-env confusion.
- Assumption to verify: pydantic-ai version in pyproject.toml — `AnthropicProvider(api_key=...)` API may differ between versions. Check the version and adjust import path / constructor signature.
- Assumption to verify: existing `LLMRouter` callers in the codebase — any direct instantiation that doesn't go through the loader path? They will break when signature changes. Audit and update.
- Watch for: `SecondSightConfig` is currently `frozen=True` dataclass. Adding `resolved_provider_keys` to it means either (a) un-freezing it (don't), (b) creating a new aggregating type `SecondSightContext` that wraps `SecondSightConfig` + `resolved_keys`, or (c) returning a tuple from the loader. Pick one and document.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DC7 (no implicit env fallback)
- DC8 (cache-once resolved keys)
- DC4 (both providers fail — partial; the primary+fallback both-fail error aggregation is here)
- "Happy path — mode=sdk + valid Anthropic key + primary succeeds"
- "SDK fallback engaged when primary fails but recovers via fallback" (degradation)
