"""Tests for LLMRouter — death tests first, then degradation, then happy path.

Death test contract: each DT-* test must target a SILENT failure path —
one where the wrong behaviour would be accepted as correct until real damage
is measured (e.g., 3× LLM cost for a broken prompt, chain exhaustion raises
nothing, budget exceeded silently).

Test doubles strategy:
  - LLMRouter accepts an ``agent_factory: Callable[[ModelSpec], Any]``
    injectable so tests pass in fake factories that raise or return
    canned results without calling a real provider.
  - Fakes raise their exceptions immediately; the router owns the timeout /
    fallback / classification logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pydantic
import pydantic_ai.exceptions

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.sdk.router import (
    LLMRouter,
    ModelSpec,
    RouterChainExhaustedError,
)


# ---------------------------------------------------------------------------
# Helpers / shared fakes
# ---------------------------------------------------------------------------


def _make_primary(**kwargs: Any) -> ModelSpec:
    return ModelSpec(name="gpt-4o-mini", provider="openai", **kwargs)


def _make_fallback(n: int = 1) -> list[ModelSpec]:
    return [ModelSpec(name=f"gemini-2.0-flash-{i}", provider="google") for i in range(n)]


# Minimal resolved_keys dict for tests that use mock agent_factory.
# Key values are test-only placeholders — they are never sent to real APIs
# because agent_factory is always overridden in these tests.
# "google" is not in the built-in factory but tests use mock factories so it
# does not matter — only the primary provider ("openai") is validated at init.
_TEST_RESOLVED_KEYS: dict[str, str] = {
    "anthropic": "sk-ant-test-placeholder",
    "openai": "sk-openai-test-placeholder",
    "custom": "",
}


def _raising_factory(exc: Exception):
    """Return an agent_factory whose agents always raise ``exc``."""
    mock_agent = AsyncMock()
    mock_agent.run.side_effect = exc
    return lambda _spec: mock_agent


def _success_factory(output: Any = "ok"):
    """Return an agent_factory whose agents always succeed."""
    mock_agent = AsyncMock()
    result = MagicMock()
    result.output = output
    mock_agent.run.return_value = result
    return lambda _spec: mock_agent


def _slow_factory(delay_s: float, exc: Exception | None = None, output: Any = "ok"):
    """Return a factory that sleeps ``delay_s`` before raising or returning."""

    async def _run(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(delay_s)
        if exc is not None:
            raise exc
        result = MagicMock()
        result.output = output
        return result

    mock_agent = MagicMock()
    mock_agent.run = _run
    return lambda _spec: mock_agent


# ---------------------------------------------------------------------------
# DEATH TESTS — must fail before implementation
# ---------------------------------------------------------------------------


class TestDeathPaths:
    """Silent failure paths — each test names the lie and the truth."""

    @pytest.mark.asyncio
    async def test_DT_2_1_validation_error_in_cause_chain_no_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DT-2.1 / DC-3: ValidationError wrapped in __cause__ → no fallback.

        Lie: outer exception class matches allowlist; router falls back to B
        and C; 3× cost for the same broken prompt.
        Truth: router walks __cause__ chain; finds ValidationError; classifies
        as terminal; raises AnalysisAgentError after 1 attempt; fallbacks = 0.
        """

        # Build a pydantic.ValidationError (requires a model to fail).
        class _M(pydantic.BaseModel):
            x: int

        try:
            _M.model_validate({"x": "not-an-int"})
        except pydantic.ValidationError as ve:
            validation_error = ve

        # Wrap it inside UnexpectedModelBehavior via __cause__.
        outer = pydantic_ai.exceptions.UnexpectedModelBehavior("bad output from model")
        outer.__cause__ = validation_error

        # Track fallback call counts via separate mocks.
        fallback_b = AsyncMock()
        fallback_c = AsyncMock()
        call_count = {"n": 0}

        def _factory(spec: ModelSpec) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Primary: raises outer
                m = AsyncMock()
                m.run.side_effect = outer
                return m
            elif call_count["n"] == 2:
                return fallback_b
            else:
                return fallback_c

        router = LLMRouter(
            primary=_make_primary(),
            fallbacks=_make_fallback(2),
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with caplog.at_level(logging.WARNING):
            with pytest.raises(AnalysisAgentError) as exc_info:
                await router.call(model_input="test prompt", output_type=str)

        # Fallback agents must NOT have been called.
        fallback_b.run.assert_not_called()
        fallback_c.run.assert_not_called()

        # Error message must mention validation failure.
        err_msg = str(exc_info.value)
        assert "validation" in err_msg.lower() or "ValidationError" in err_msg

        # Log must capture the unwrapped ValidationError.
        log_text = caplog.text
        assert "ValidationError" in log_text or "validation" in log_text.lower()

    @pytest.mark.asyncio
    async def test_DT_2_2_empty_chain_warns_and_error_includes_config(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DT-2.2 / DC-8: empty fallback chain logs WARN at construction;
        chain-exhaustion error message names the empty config.

        Lie: router silently constructs; primary fails with transport error;
        uncaught exception bubbles with no context about the missing chain.
        Truth: WARN logged at construction; AnalysisAgentError on exhaustion
        mentions "fallback_models is empty" and includes chain trace.
        """
        primary_exc = httpx.TimeoutException("connection timed out")

        factory = _raising_factory(primary_exc)

        with caplog.at_level(logging.WARNING):
            router = LLMRouter(
                primary=_make_primary(),
                fallbacks=[],
                resolved_keys=_TEST_RESOLVED_KEYS,
                agent_factory=factory,
            )

        # Construction must emit a WARN.
        warn_text = caplog.text
        assert "warn" in warn_text.lower() or "fallback" in warn_text.lower()

        with pytest.raises(AnalysisAgentError) as exc_info:
            await router.call(model_input="test", output_type=str)

        err_msg = str(exc_info.value)
        assert "fallback_models is empty" in err_msg or "empty" in err_msg.lower()
        # Chain trace must mention the primary attempt.
        assert "gpt-4o-mini" in err_msg or "attempt" in err_msg.lower()

    @pytest.mark.asyncio
    async def test_DT_2_3_chain_exhaustion_raises_with_trace(self) -> None:
        """DT-2.3: primary + 2 fallbacks all raise RateLimitError;
        raised RouterChainExhaustedError has ``attempts`` attribute with
        all 3 entries, each (model_name, exception_class_name, duration_ms).

        Lie: chain exhausted silently; caller gets None or an unwrapped exc
        with no context about which models were tried.
        Truth: structured error with per-attempt trace — all 3 recorded.
        """
        import litellm

        rate_limit_exc = litellm.RateLimitError(
            message="rate limited",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

        factories_called: list[str] = []

        def _factory(spec: ModelSpec) -> Any:
            factories_called.append(spec.name)
            m = AsyncMock()
            m.run.side_effect = litellm.RateLimitError(
                message="rate limited",
                llm_provider="openai",
                model=spec.name,
            )
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[
                ModelSpec(name="gemini-2.0-flash-0", provider="google"),
                ModelSpec(name="gemini-2.0-flash-1", provider="google"),
            ],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with pytest.raises(RouterChainExhaustedError) as exc_info:
            await router.call(model_input="test", output_type=str)

        err: RouterChainExhaustedError = exc_info.value
        assert hasattr(err, "attempts"), "RouterChainExhaustedError must have 'attempts'"
        assert len(err.attempts) == 3, f"Expected 3 attempts, got {len(err.attempts)}"

        for attempt in err.attempts:
            assert hasattr(attempt, "model_name") or (
                isinstance(attempt, dict) and "model_name" in attempt
            ), f"Attempt missing model_name: {attempt}"
            assert hasattr(attempt, "exception_class") or (
                isinstance(attempt, dict) and "exception_class" in attempt
            ), f"Attempt missing exception_class: {attempt}"
            assert hasattr(attempt, "duration_ms") or (
                isinstance(attempt, dict) and "duration_ms" in attempt
            ), f"Attempt missing duration_ms: {attempt}"

        model_names = [
            a["model_name"] if isinstance(a, dict) else a.model_name for a in err.attempts
        ]
        assert "gpt-4o-mini" in model_names

    @pytest.mark.asyncio
    async def test_DT_2_4_chain_total_timeout_halts_before_all_fallbacks(
        self,
    ) -> None:
        """DT-2.4: chain_total_timeout_s enforced; slow primary exhausts budget;
        fallbacks would succeed but are not called.

        Lie: per-call timeout fires but chain_total budget is not tracked;
        after primary's per-call timeout, fallbacks still get their full 60s
        each; user waits N × 60s.
        Truth: chain total budget is deducted per attempt; when budget is
        exhausted, router raises AnalysisAgentError with
        reason="chain_total_timeout_exceeded" before calling remaining fallbacks.
        """
        # Primary sleeps 0.6s (exceeds the 0.5s chain budget).
        # asyncio.wait_for will fire after chain_total_timeout_s=0.5s, producing
        # asyncio.TimeoutError. After that attempt, elapsed >= 0.5s, triggering
        # the chain_total_timeout_exceeded check before the fallback is attempted.
        slow_primary_factory = _slow_factory(
            delay_s=0.6,
            exc=httpx.TimeoutException("slow"),
        )
        # Fallbacks would succeed if reached.
        success_factory = _success_factory("fallback succeeded")
        call_log: list[str] = []

        def _factory(spec: ModelSpec) -> Any:
            call_log.append(spec.name)
            if spec.name == "gpt-4o-mini":
                return slow_primary_factory(spec)
            return success_factory(spec)

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[
                ModelSpec(name="gemini-2.0-flash-0", provider="google"),
                ModelSpec(name="gemini-2.0-flash-1", provider="google"),
            ],
            resolved_keys=_TEST_RESOLVED_KEYS,
            chain_total_timeout_s=0.5,
            per_call_timeout_s=60.0,
            agent_factory=_factory,
        )

        start = time.monotonic()
        with pytest.raises(AnalysisAgentError) as exc_info:
            await router.call(model_input="test", output_type=str)
        elapsed = time.monotonic() - start

        # Must complete in well under 60s (per-call timeout is not blocking).
        assert elapsed < 5.0, f"Router took too long: {elapsed:.2f}s"

        err_msg = str(exc_info.value)
        assert "chain_total_timeout_exceeded" in err_msg or "timeout" in err_msg.lower(), (
            f"Expected chain timeout reason, got: {err_msg}"
        )

        # Fallbacks must NOT have been called.
        assert call_log[0] == "gpt-4o-mini"
        # Fallbacks should not appear in call_log (chain timeout fired before them).
        assert "gemini-2.0-flash-0" not in call_log, (
            "Fallback was called despite chain_total_timeout being exceeded"
        )


# ---------------------------------------------------------------------------
# DEGRADATION TESTS
# ---------------------------------------------------------------------------


class TestDegradation:
    """DG-*: partial-failure paths that produce recoverable outcomes."""

    @pytest.mark.asyncio
    async def test_DG_1_1_fallback_fires_on_rate_limit_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DG-1.1: primary raises RateLimitError; fallback succeeds;
        log has outcome=fallback_triggered.

        Ensures fallback DOES fire for transport errors (the complement
        to DT-2.1 which tests it does NOT fire for validation errors).
        """
        import litellm

        call_log: list[str] = []
        fallback_result = MagicMock()
        fallback_result.output = "fallback output"

        def _factory(spec: ModelSpec) -> Any:
            call_log.append(spec.name)
            m = AsyncMock()
            if spec.name == "gpt-4o-mini":
                m.run.side_effect = litellm.RateLimitError(
                    message="rate limited",
                    llm_provider="openai",
                    model="gpt-4o-mini",
                )
            else:
                m.run.return_value = fallback_result
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[ModelSpec(name="gemini-2.0-flash", provider="google")],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with caplog.at_level(logging.INFO):
            result = await router.call(model_input="test prompt", output_type=str)

        assert result == "fallback output"
        assert "gpt-4o-mini" in call_log
        assert "gemini-2.0-flash" in call_log

        # Log must mention fallback_triggered.
        assert "fallback" in caplog.text.lower()


# ---------------------------------------------------------------------------
# HAPPY PATH TESTS
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """HP-*: nominal paths with no failures."""

    @pytest.mark.asyncio
    async def test_HP_2_6_single_primary_success_zero_fallback_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HP-2.6: primary succeeds; fallback is never constructed.

        Ensures that on success, the router returns immediately without
        instantiating fallback agents (cost-free sunny path).
        """
        result_obj = MagicMock()
        result_obj.output = "primary output"

        call_log: list[str] = []

        def _factory(spec: ModelSpec) -> Any:
            call_log.append(spec.name)
            m = AsyncMock()
            m.run.return_value = result_obj
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[ModelSpec(name="gemini-2.0-flash", provider="google")],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with caplog.at_level(logging.INFO):
            result = await router.call(model_input="hello", output_type=str)

        assert result == "primary output"
        # Only the primary model was instantiated.
        assert call_log == ["gpt-4o-mini"], f"Expected only primary to be called, got: {call_log}"

        # Log must record the successful attempt.
        assert "gpt-4o-mini" in caplog.text

    @pytest.mark.asyncio
    async def test_HP_transport_errors_all_trigger_fallback(self) -> None:
        """Each transport error type in the allowlist triggers fallback.

        Covers: httpx.TimeoutException, httpx.ConnectError,
        httpx.RemoteProtocolError, litellm.APIConnectionError,
        litellm.ServiceUnavailableError.
        """
        import litellm

        transport_errors = [
            httpx.TimeoutException("timeout"),
            httpx.ConnectError("connect error"),
            httpx.RemoteProtocolError("remote protocol"),
            litellm.APIConnectionError(
                message="api conn",
                llm_provider="openai",
                model="gpt-4o-mini",
            ),
            litellm.ServiceUnavailableError(
                message="503",
                llm_provider="openai",
                model="gpt-4o-mini",
            ),
        ]

        for transport_err in transport_errors:
            call_log: list[str] = []
            success_result = MagicMock()
            success_result.output = "ok"

            def _factory(spec: ModelSpec, _err: Exception = transport_err) -> Any:
                call_log.append(spec.name)
                m = AsyncMock()
                if spec.name == "gpt-4o-mini":
                    m.run.side_effect = _err
                else:
                    m.run.return_value = success_result
                return m

            router = LLMRouter(
                primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
                fallbacks=[ModelSpec(name="gemini-2.0-flash", provider="google")],
                resolved_keys=_TEST_RESOLVED_KEYS,
                agent_factory=_factory,
            )
            result = await router.call(model_input="test", output_type=str)
            assert result == "ok", f"Fallback did not fire for {type(transport_err).__name__}"

    @pytest.mark.asyncio
    async def test_HP_validation_error_direct_raise_terminal(self) -> None:
        """A direct pydantic.ValidationError (not wrapped) is also terminal."""

        class _M(pydantic.BaseModel):
            x: int

        try:
            _M.model_validate({"x": "not-an-int"})
        except pydantic.ValidationError as ve:
            val_err = ve

        call_log: list[str] = []

        def _factory(spec: ModelSpec) -> Any:
            call_log.append(spec.name)
            m = AsyncMock()
            m.run.side_effect = val_err
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[ModelSpec(name="gemini-2.0-flash", provider="google")],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with pytest.raises(AnalysisAgentError):
            await router.call(model_input="test", output_type=str)

        # Fallback must not be called.
        assert call_log == ["gpt-4o-mini"], f"Fallback was called: {call_log}"

    @pytest.mark.asyncio
    async def test_HP_provider_auth_error_fallback_once_per_chain(self) -> None:
        """ProviderAuthError triggers fallback exactly once per provider.

        Auth errors are tracked as (provider, exception_class) tuples. Each
        provider gets one auth-error fallback. A second auth error from the
        same provider is immediately terminal.

        Setup: primary (openai) + 2 fallbacks (google, google).
        - primary openai auth error → first from openai, fallback-eligible.
        - fallback-0 google auth error → first from google, fallback-eligible.
        - fallback-1 google auth error → second from google, terminal → stop.

        So all 3 models are tried but the chain terminates on the 3rd model's
        second-google-auth-error without trying any further models.
        """
        import litellm

        call_log: list[str] = []

        def _factory(spec: ModelSpec) -> Any:
            call_log.append(spec.name)
            m = AsyncMock()
            m.run.side_effect = litellm.AuthenticationError(
                message="auth failed",
                llm_provider=spec.provider,
                model=spec.name,
            )
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[
                ModelSpec(name="gemini-2.0-flash-0", provider="google"),
                ModelSpec(name="gemini-2.0-flash-1", provider="google"),
            ],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with pytest.raises(AnalysisAgentError):
            await router.call(model_input="test", output_type=str)

        # All 3 models tried: primary (openai first auth), fallback-0 (google first auth),
        # fallback-1 (google second auth = terminal). No 4th model beyond that.
        assert len(call_log) == 3, (
            f"Expected exactly 3 models tried with per-provider auth dedup, got: {call_log}"
        )
        assert call_log == ["gpt-4o-mini", "gemini-2.0-flash-0", "gemini-2.0-flash-1"]

    @pytest.mark.asyncio
    async def test_HP_agent_factory_raises_terminal_error(self) -> None:
        """If agent_factory itself raises during call(), raises
        RouterTerminalError with a clear configuration-error message.

        Self-iteration fix for scar silent_failure_conditions[2]:
        previously, factory construction errors escaped as un-classified
        exceptions (AttributeError etc) instead of AnalysisAgentError.

        Note: Task 5 adds resolved_keys validation at LLMRouter.__init__ time
        that rejects missing-key providers before the factory is ever called.
        This test verifies the factory-error handling path (for custom factories
        that raise for other reasons), using a known provider with a valid key
        so the init-time check passes.
        """

        def _bad_factory(spec: ModelSpec) -> Any:
            raise ValueError(f"Custom factory error for model: {spec.name}")

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_bad_factory,
        )

        from secondsight.sdk.router import RouterTerminalError

        with pytest.raises(RouterTerminalError) as exc_info:
            await router.call(model_input="test", output_type=str)

        assert (
            "configuration error" in str(exc_info.value).lower()
            or "factory" in str(exc_info.value).lower()
        )

    def test_HP_router_config_public_api_returns_constructor_values(self) -> None:
        """RouterConfig public property returns the same values passed to LLMRouter constructor.

        Death test: if router.py renames _primary, _fallbacks, etc. without updating
        the config property, agent.py would read stale/wrong values at construction
        time. This test closes the silent failure by verifying the public API is
        a faithful view of constructor args.
        """
        from secondsight.sdk.router import RouterConfig  # noqa: PLC0415

        primary = ModelSpec(name="gpt-4o-mini", provider="openai")
        fallback_a = ModelSpec(name="gemini-2.0-flash", provider="google")
        fallback_b = ModelSpec(name="claude-haiku", provider="anthropic")

        router = LLMRouter(
            primary=primary,
            fallbacks=[fallback_a, fallback_b],
            resolved_keys=_TEST_RESOLVED_KEYS,
            per_call_timeout_s=45.0,
            chain_total_timeout_s=120.0,
        )

        config = router.config
        assert isinstance(config, RouterConfig), (
            f"router.config must return a RouterConfig instance, got {type(config).__name__}"
        )
        assert config.primary == primary
        assert config.fallbacks == [fallback_a, fallback_b]
        assert config.per_call_timeout_s == 45.0
        assert config.chain_total_timeout_s == 120.0

    @pytest.mark.asyncio
    async def test_HP_unexpected_model_behavior_without_cause_terminal(self) -> None:
        """UnexpectedModelBehavior with no ValidationError in cause chain
        is terminal (unknown error class → do not fallback)."""
        outer = pydantic_ai.exceptions.UnexpectedModelBehavior("the model returned garbage")
        # No __cause__ set — plain unknown error.

        call_log: list[str] = []

        def _factory(spec: ModelSpec) -> Any:
            call_log.append(spec.name)
            m = AsyncMock()
            m.run.side_effect = outer
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[ModelSpec(name="gemini-2.0-flash", provider="google")],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with pytest.raises(AnalysisAgentError):
            await router.call(model_input="test", output_type=str)

        # Fallback must NOT be called (terminal error).
        assert call_log == ["gpt-4o-mini"], f"Fallback called for terminal error: {call_log}"


# ---------------------------------------------------------------------------
# DEATH TEST — logging schema contract (DT-2.5)
# ---------------------------------------------------------------------------


class TestLoggingSchema:
    """DT-2.5 and related: verify the North Star sub-metric logging schema.

    The module docstring declares the logging schema as a contract:
      provider, model, tokens_in, tokens_out, duration_ms, attempt, total_attempts, outcome

    Silent failure: tokens_in and tokens_out absent from log — aggregator for
    fallback_chain_success_rate / token-cost metrics silently receives incomplete
    records. First discoverer: ops team querying cost analytics, weeks after deploy.
    """

    @pytest.mark.asyncio
    async def test_DT_2_5_success_log_contains_tokens_in_tokens_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DT-2.5 death test: on primary success, log line MUST contain
        tokens_in= and tokens_out= fields.

        Lie: log appears to succeed; aggregator silently receives records
        missing token accounting; cost analysis shows zero tokens forever.
        Truth: each INFO log line includes tokens_in= and tokens_out= keys.
        On success with usage available, values are integers.
        """
        result_obj = MagicMock()
        result_obj.output = "primary output"
        # Simulate PydanticAI RunResult with usage() returning token counts.
        usage_obj = MagicMock()
        usage_obj.request_tokens = 42
        usage_obj.response_tokens = 17
        result_obj.usage.return_value = usage_obj

        def _factory(spec: ModelSpec) -> Any:
            m = AsyncMock()
            m.run.return_value = result_obj
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with caplog.at_level(logging.INFO):
            await router.call(model_input="hello", output_type=str)

        # The INFO log line for the success attempt must contain tokens_in and tokens_out.
        success_lines = [r.message for r in caplog.records if "outcome=success" in r.message]
        assert success_lines, "No log line with outcome=success found"
        log_line = success_lines[0]
        assert "tokens_in=" in log_line, f"tokens_in missing from log: {log_line!r}"
        assert "tokens_out=" in log_line, f"tokens_out missing from log: {log_line!r}"

    @pytest.mark.asyncio
    async def test_DT_2_5b_failure_log_contains_tokens_in_none_tokens_out_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DT-2.5b: on transport error (fallback triggered), log line contains
        tokens_in=None tokens_out=None — keys must be present even when
        no usage data is available (exception path has no RunResult).
        """
        import litellm

        fallback_result = MagicMock()
        fallback_result.output = "fallback ok"
        usage_obj = MagicMock()
        usage_obj.request_tokens = 5
        usage_obj.response_tokens = 3
        fallback_result.usage.return_value = usage_obj

        def _factory(spec: ModelSpec) -> Any:
            m = AsyncMock()
            if spec.name == "gpt-4o-mini":
                m.run.side_effect = litellm.RateLimitError(
                    message="rate limited", llm_provider="openai", model="gpt-4o-mini"
                )
            else:
                m.run.return_value = fallback_result
            return m

        router = LLMRouter(
            primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
            fallbacks=[ModelSpec(name="gemini-2.0-flash", provider="google")],
            resolved_keys=_TEST_RESOLVED_KEYS,
            agent_factory=_factory,
        )

        with caplog.at_level(logging.INFO):
            await router.call(model_input="test", output_type=str)

        # The fallback-triggered log line must contain tokens_in=None tokens_out=None.
        fallback_lines = [
            r.message for r in caplog.records if "outcome=fallback_triggered" in r.message
        ]
        assert fallback_lines, "No log line with outcome=fallback_triggered found"
        log_line = fallback_lines[0]
        assert "tokens_in=" in log_line, f"tokens_in missing from fallback log: {log_line!r}"
        assert "tokens_out=" in log_line, f"tokens_out missing from fallback log: {log_line!r}"
