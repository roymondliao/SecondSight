"""LLMRouter — transport-error-only fallback chain wrapping PydanticAI agents.

Architecture decisions from 2-plan.md:
  D5. Fallback fires ONLY on transport-error allowlist. ValidationError and
      UnexpectedModelBehavior are terminal — they bubble as AnalysisAgentError
      and never trigger fallback. This is the cost-leak control.
  D6. Direct PydanticAI provider per model. LiteLLM is the escape hatch for
      non-OpenAI-compatible providers, NOT the routing layer. We own fallback
      policy fully.
  D11. Default fallback chain: ["gpt-4o-mini", "gemini-2.0-flash"].
  D13. Per-project fallback override deferred. v1 has only the global chain.

Death cases closed here:
  DC-3: ValidationError masquerading as transport error. _classify() walks
        BOTH __cause__ AND __context__ chains. At any depth: pydantic.ValidationError
        → terminal. This prevents 3× cost for the same broken prompt.
  DC-8: Empty fallback chain logs WARN at construction. On primary failure
        with empty chain, raised AnalysisAgentError names "fallback_models is
        empty" and includes the primary attempt trace.

Logging schema (North Star sub-metric — this format must NOT change silently):
  Every attempt emits an INFO log with structured fields:
    provider, model, tokens_in, tokens_out, duration_ms, attempt, total_attempts, outcome
  The aggregator fallback_chain_success_rate sub-metric depends on this exact format.
  outcome values: "success" | "fallback_triggered" | "terminal_error" |
                  "chain_exhausted" | "chain_total_timeout_exceeded"
  tokens_in / tokens_out: extracted from RunResult.usage() on success; None on
  exception paths where no RunResult is available.

ProviderAuthError fallback semantics:
  AuthenticationError is fallback-eligible on the FIRST encounter per call.
  A per-call _seen_auth_errors set tracks (provider, exception_class) tuples.
  On the second auth error (same or different provider), the error is terminal.
  IMPORTANT: _seen_auth_errors is initialized in each call() invocation,
  NOT at constructor level, to prevent leakage across separate router.call()
  invocations (scar item: verified per-call scope).

Exception chain walking:
  Python has TWO exception-chain mechanisms:
    exc.__cause__   — explicit "raise X from Y" (preferred, tested in DT-2.1)
    exc.__context__ — implicit chaining when an exception is raised inside
                      an except block (not tested explicitly but handled here)
  _classify() walks both chains to maximum depth _MAX_CHAIN_DEPTH before
  falling through to the default (terminal) classification.
"""

from __future__ import annotations

import asyncio
from loguru import logger
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
import litellm
import pydantic
import pydantic_ai
import pydantic_ai.exceptions
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.sdk._specs import ModelSpec

# Maximum depth for walking __cause__ / __context__ chains in _classify().
# Prevents infinite-loop on pathological exception graphs.
_MAX_CHAIN_DEPTH: int = 20

OutputT = TypeVar("OutputT")


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

# ModelSpec is the single source of truth defined in _specs.py.
# Do NOT redefine it here. Import above handles this.


@dataclass(frozen=True)
class RouterConfig:
    """Frozen view of LLMRouter's construction-time configuration.

    Exposed via ``LLMRouter.config`` property as the public read API.
    Consumers (e.g., PydanticAIAnalysisAgent) must use this instead of
    accessing ``router._primary``, ``router._fallbacks``, etc. directly.

    Attributes:
        primary: The primary ModelSpec (tried first).
        fallbacks: Ordered list of fallback ModelSpecs (tried in sequence).
        per_call_timeout_s: Per-attempt timeout in seconds.
        chain_total_timeout_s: Total budget across all attempts in seconds.

    Design note: agent_factory is intentionally NOT included in RouterConfig.
    The factory is an implementation detail of the router's call mechanism;
    consumers (PydanticAIAnalysisAgent) build their own scoped factories and
    do not need access to the router's factory.
    """

    primary: ModelSpec
    fallbacks: list[ModelSpec]
    per_call_timeout_s: float
    chain_total_timeout_s: float


@dataclass
class AttemptRecord:
    """One attempt in the chain trace.

    Used in RouterChainExhaustedError.attempts and in DT-2.3 assertions.
    """

    model_name: str
    exception_class: str
    duration_ms: float


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RouterChainExhaustedError(AnalysisAgentError):
    """All models in the chain were tried and all failed with transport errors.

    Attributes:
        attempts: List of AttemptRecord for every model tried.
                  Each record has: model_name, exception_class, duration_ms.
    """

    def __init__(self, message: str, attempts: list[AttemptRecord]) -> None:
        super().__init__(message)
        self.attempts = attempts


class RouterChainTimeoutError(AnalysisAgentError):
    """chain_total_timeout_s exceeded before the chain could complete.

    Reason "chain_total_timeout_exceeded" is embedded in the message.
    """


class RouterTerminalError(AnalysisAgentError):
    """A terminal (non-transient) error stopped the chain immediately.

    Wraps the original exception. Used for ValidationError and other
    non-fallback-eligible errors.
    """


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


class _ClassifyResult:
    """Internal enum-like result from _classify()."""

    FALLBACK_ELIGIBLE = "fallback_eligible"
    TERMINAL = "terminal"
    AUTH_ONCE = "auth_once"  # treat as fallback on first encounter only


# ---------------------------------------------------------------------------
# LLMRouter
# ---------------------------------------------------------------------------


class LLMRouter:
    """Iterates [primary] + fallbacks with transport-error-only fallback policy.

    Construction:
        router = LLMRouter(
            primary=ModelSpec("gpt-4o-mini", "openai"),
            fallbacks=[ModelSpec("gemini-2.0-flash", "google")],
        )

    Injection for testing:
        router = LLMRouter(..., agent_factory=lambda spec: my_fake_agent)

    This implementation assumes:
        1. PydanticAI Agent.run() is an async coroutine (verified: yes).
        2. litellm is importable (it is in project deps). If it is removed,
           the litellm exception isinstance() checks will fail at import time.
        3. asyncio.wait_for() honours the timeout in asyncio's event loop;
           blocking sync code inside Agent.run would bypass per_call_timeout_s.
           If Agent.run blocks the event loop, the timeout will not fire.
        4. _seen_auth_errors is reset per call() invocation (not per router
           instance) — verified in test HP_provider_auth_error_fallback_once.
    """

    def __init__(
        self,
        *,
        primary: ModelSpec,
        fallbacks: list[ModelSpec],
        resolved_keys: dict[str, str],
        per_call_timeout_s: float = 60.0,
        chain_total_timeout_s: float = 90.0,
        agent_factory: Callable[[ModelSpec], Any] | None = None,
    ) -> None:
        self._primary = primary
        self._fallbacks = fallbacks
        self._per_call_timeout_s = per_call_timeout_s
        self._chain_total_timeout_s = chain_total_timeout_s
        # Snapshot the resolved keys at construction time (DC8: cache-once contract).
        # Mid-flight os.environ mutations have NO effect after this point.
        # Key rotation requires server restart so this snapshot is re-created.
        self._resolved_keys: dict[str, str] = dict(resolved_keys)

        # Decision E1: validate that at least one key is non-empty AND that the
        # primary's provider has a configured key. Fail at construction time, not
        # at first dispatch. Silent failure mode: if we skip this check, the first
        # dispatch would get a 401 from the provider API, producing an AnalysisOutput
        # with status='failure' and no process-level exception to alert monitoring.
        self._validate_provider_keys_at_init(primary, fallbacks, self._resolved_keys)

        if agent_factory is not None:
            self._agent_factory: Callable[[ModelSpec], Any] = agent_factory
        else:
            # Build the default factory with the snapshot keys closed over.
            # This closure ensures the factory never reads os.environ directly.
            self._agent_factory = _make_explicit_agent_factory(self._resolved_keys)

        if len(fallbacks) == 0:
            logger.warning(
                f"LLMRouter constructed with empty fallback chain "
                f"(fallback_models is empty). "
                f"A transport error on the primary will raise immediately "
                f"with no retry. "
                f"primary={primary.name!r}"
            )

    @staticmethod
    def _validate_provider_keys_at_init(
        primary: ModelSpec,
        fallbacks: list[ModelSpec],
        resolved_keys: dict[str, str],
    ) -> None:
        """Validate that the primary provider has a configured key.

        Raises RouterTerminalError at construction time (not at dispatch time)
        when the primary's provider key is empty or all keys are empty.

        Decision E1: the ONLY injection path is ${VAR} interpolation in TOML.
        Empty string = not configured. We reject this here explicitly.

        Silent failure mode we're preventing: if this check were absent,
        LLMRouter would construct successfully, dispatch would fire, the provider
        would receive api_key=None or api_key="", and the API would return a 401.
        That 401 manifests as AnalysisOutput(status='failure') with no process-level
        exception — monitoring that looks for exceptions would miss it entirely.
        """
        all_empty = all(not v for v in resolved_keys.values())
        if all_empty:
            raise RouterTerminalError(
                "no provider keys resolvable: all resolved_keys are empty. "
                "Set at least one provider key via ${VAR} interpolation in config.toml "
                "[providers.*] section. "
                "Direct env fallback is disabled (Decision E1). "
                f"primary_provider={primary.provider!r}"
            )

        primary_key = resolved_keys.get(primary.provider, "")
        if not primary_key:
            raise RouterTerminalError(
                f"no provider keys resolvable: primary provider {primary.provider!r} "
                f"has empty key in resolved_keys. "
                f"Set [providers.{primary.provider}] key via ${{VAR}} interpolation. "
                f"Direct env fallback is disabled (Decision E1). "
                f"primary_model={primary.name!r}"
            )

    @property
    def config(self) -> RouterConfig:
        """Return a frozen view of this router's construction-time configuration.

        Consumers should use this instead of accessing private attributes directly.
        agent_factory is intentionally excluded — see RouterConfig docstring.
        """
        return RouterConfig(
            primary=self._primary,
            fallbacks=self._fallbacks,
            per_call_timeout_s=self._per_call_timeout_s,
            chain_total_timeout_s=self._chain_total_timeout_s,
        )

    @property
    def resolved_keys(self) -> dict[str, str]:
        """Snapshot of resolved provider keys at router construction time.

        Returns a copy to prevent external mutation of the internal snapshot.
        This is the DC8 cache-once contract: the snapshot is fixed at construction;
        callers who need the keys should access them here, not via os.environ.

        Use this property instead of accessing router._resolved_keys directly.
        """
        return dict(self._resolved_keys)

    async def call(
        self,
        *,
        model_input: Any,
        output_type: type[Any],
    ) -> Any:
        """Run the primary; fall back to each model in order on transport errors.

        Returns the .output attribute of the first successful AgentRunResult.

        Raises:
            RouterTerminalError    — validation error or other non-transient failure.
            RouterChainExhaustedError — all models tried; all raised transport errors.
            RouterChainTimeoutError   — chain_total_timeout_s exceeded.
        """
        chain: list[ModelSpec] = [self._primary] + self._fallbacks
        total_models = len(chain)
        attempts: list[AttemptRecord] = []

        # Per-call auth-error sentinel (not per-router-instance — must not leak).
        # Tracks (provider, exception_class_name) tuples. Auth errors are
        # deduplicated per-provider so that openai's AuthenticationError and
        # google's AuthenticationError each get one fallback chance. A second
        # auth error from the same provider is terminal immediately.
        # NOTE: because litellm.AuthenticationError covers all OpenAI-compatible
        # providers via a single exception class, in practice any second auth error
        # terminates the chain.
        _seen_auth_errors: set[tuple[str, str]] = set()

        chain_start = time.monotonic()

        for attempt_idx, spec in enumerate(chain):
            # Check chain total budget before attempting this model.
            elapsed = time.monotonic() - chain_start
            remaining_budget = self._chain_total_timeout_s - elapsed
            if remaining_budget <= 0 and attempt_idx > 0:
                # Budget exhausted before this attempt (not on first attempt
                # — if budget is already 0 at start, try at least the primary).
                logger.warning(
                    f"chain_total_timeout_exceeded before attempt {attempt_idx + 1}/{total_models} "
                    f"provider={spec.provider!r} model={spec.name!r} "
                    f"elapsed_s={elapsed:.3f} budget_s={self._chain_total_timeout_s:.3f}"
                )
                raise RouterChainTimeoutError(
                    f"chain_total_timeout_exceeded: {elapsed:.3f}s elapsed, "
                    f"budget was {self._chain_total_timeout_s}s. "
                    f"Completed {attempt_idx} of {total_models} attempts."
                )

            try:
                agent = self._agent_factory(spec)
            except Exception as factory_exc:
                # agent_factory itself raised (e.g., unknown provider name).
                # This is a configuration error — terminal, not transient.
                logger.error(
                    f"agent_factory raised during construction "
                    f"provider={spec.provider!r} model={spec.name!r} attempt={attempt_idx + 1} "
                    f"exc_type={type(factory_exc).__name__!r} exc={factory_exc}"
                )
                raise RouterTerminalError(
                    f"terminal_error: agent_factory raised for model={spec.name!r} "
                    f"({type(factory_exc).__name__}: {factory_exc}). "
                    f"This is a configuration error — check provider name and API key env."
                ) from factory_exc

            call_start = time.monotonic()

            try:
                result = await asyncio.wait_for(
                    agent.run(model_input, output_type=output_type),
                    timeout=min(self._per_call_timeout_s, max(remaining_budget, 0.001)),
                )
                duration_ms = (time.monotonic() - call_start) * 1000

                # Best-effort token extraction from RunResult.usage().
                usage = (
                    result.usage()
                    if callable(getattr(result, "usage", None))
                    else getattr(result, "usage", None)
                )
                tokens_in = usage.request_tokens if usage is not None else None
                tokens_out = usage.response_tokens if usage is not None else None

                logger.info(
                    f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                    f"tokens_in={tokens_in} tokens_out={tokens_out} "
                    f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                    f"total_attempts={total_models} outcome=success"
                )
                return result.output

            except asyncio.TimeoutError:
                duration_ms = (time.monotonic() - call_start) * 1000
                attempts.append(
                    AttemptRecord(
                        model_name=spec.name,
                        exception_class="TimeoutError",
                        duration_ms=duration_ms,
                    )
                )
                logger.info(
                    f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                    f"tokens_in={None} tokens_out={None} "
                    f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                    f"total_attempts={total_models} outcome=fallback_triggered "
                    f"reason=per_call_timeout"
                )
                # asyncio.TimeoutError is a transport error — continue fallback.

            except Exception as exc:
                duration_ms = (time.monotonic() - call_start) * 1000
                classify_result = _classify(exc, _seen_auth_errors)

                if classify_result == _ClassifyResult.TERMINAL:
                    # Find and log the root ValidationError if present (DC-3 diagnosis).
                    root_validation = _find_validation_error(exc)
                    if root_validation is not None:
                        logger.warning(
                            f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                            f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                            f"total_attempts={total_models} outcome=terminal_error "
                            f"exc_type={type(exc).__name__!r} reason=ValidationError_in_chain "
                            f"validation_error={root_validation}"
                        )
                        raise RouterTerminalError(
                            f"terminal_error: ValidationError found in exception chain "
                            f"(DC-3 cost-leak prevention, not retrying). "
                            f"outer_exc={type(exc).__name__!r} "
                            f"validation_error={root_validation!s} "
                            f"model={spec.name!r} attempt={attempt_idx + 1}/{total_models}"
                        ) from exc
                    else:
                        logger.warning(
                            f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                            f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                            f"total_attempts={total_models} outcome=terminal_error "
                            f"exc_type={type(exc).__name__!r} exc={exc}"
                        )
                        raise RouterTerminalError(
                            f"terminal_error: {type(exc).__name__}: {exc}. "
                            f"model={spec.name!r} attempt={attempt_idx + 1}/{total_models}"
                        ) from exc

                elif classify_result == _ClassifyResult.AUTH_ONCE:
                    auth_key = (spec.provider, type(exc).__name__)
                    if auth_key in _seen_auth_errors:
                        # Second auth error in this chain → terminal.
                        logger.warning(
                            f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                            f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                            f"total_attempts={total_models} outcome=terminal_error "
                            f"reason=repeated_auth_error exc_type={type(exc).__name__!r}"
                        )
                        attempts.append(
                            AttemptRecord(
                                model_name=spec.name,
                                exception_class=type(exc).__name__,
                                duration_ms=duration_ms,
                            )
                        )
                        raise RouterChainExhaustedError(
                            _build_exhausted_message(
                                attempts=attempts,
                                fallbacks=self._fallbacks,
                            ),
                            attempts=attempts,
                        ) from exc
                    else:
                        # First auth error → treat as fallback-eligible.
                        _seen_auth_errors.add(auth_key)
                        attempts.append(
                            AttemptRecord(
                                model_name=spec.name,
                                exception_class=type(exc).__name__,
                                duration_ms=duration_ms,
                            )
                        )
                        logger.info(
                            f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                            f"tokens_in={None} tokens_out={None} "
                            f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                            f"total_attempts={total_models} outcome=fallback_triggered "
                            f"reason=auth_error_once"
                        )

                else:
                    # FALLBACK_ELIGIBLE transport error.
                    attempts.append(
                        AttemptRecord(
                            model_name=spec.name,
                            exception_class=type(exc).__name__,
                            duration_ms=duration_ms,
                        )
                    )
                    logger.info(
                        f"llm_attempt provider={spec.provider!r} model={spec.name!r} "
                        f"tokens_in={None} tokens_out={None} "
                        f"duration_ms={duration_ms:.1f} attempt={attempt_idx + 1} "
                        f"total_attempts={total_models} outcome=fallback_triggered "
                        f"reason={type(exc).__name__!r}"
                    )

            # Check chain total budget AFTER this attempt.
            elapsed_after = time.monotonic() - chain_start
            if elapsed_after >= self._chain_total_timeout_s and attempt_idx < len(chain) - 1:
                logger.warning(
                    f"chain_total_timeout_exceeded after attempt {attempt_idx + 1}/{total_models} "
                    f"provider={spec.provider!r} model={spec.name!r} "
                    f"elapsed_s={elapsed_after:.3f} budget_s={self._chain_total_timeout_s:.3f}"
                )
                raise RouterChainTimeoutError(
                    f"chain_total_timeout_exceeded: {elapsed_after:.3f}s elapsed after "
                    f"attempt {attempt_idx + 1}, budget was {self._chain_total_timeout_s}s. "
                    f"Halting before remaining fallbacks."
                )

        # All models exhausted.
        raise RouterChainExhaustedError(
            _build_exhausted_message(attempts=attempts, fallbacks=self._fallbacks),
            attempts=attempts,
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify(exc: BaseException, seen_auth_errors: set[tuple[str, str]]) -> str:
    """Classify ``exc`` into fallback_eligible | auth_once | terminal.

    Walk order:
      1. Walk BOTH __cause__ AND __context__ chains (up to _MAX_CHAIN_DEPTH).
         At ANY depth: pydantic.ValidationError → terminal.
         This closes DC-3 (ValidationError wrapped inside UnexpectedModelBehavior).

      2. Check the outermost exception against the transport-error allowlist.

      3. Check for ProviderAuthError (litellm.AuthenticationError or
         openai.AuthenticationError) → auth_once (handled by caller).

      4. Default: terminal (unknown_error).

    Note on __context__ vs __cause__:
      - exc.__cause__   is set by "raise X from Y" (explicit chaining).
      - exc.__context__ is set when an exception is raised inside an except
        block without explicit "from" (implicit chaining).
      Both are walked to ensure DC-3 is caught regardless of which chaining
      mechanism the LLM provider library uses.
    """
    # Step 1: scan the full exception graph for ValidationError.
    if _find_validation_error(exc) is not None:
        return _ClassifyResult.TERMINAL

    # Step 2: check outermost against transport-error allowlist.
    if _is_transport_error(exc):
        return _ClassifyResult.FALLBACK_ELIGIBLE

    # Step 3: check for auth error.
    if _is_auth_error(exc):
        return _ClassifyResult.AUTH_ONCE

    # Step 4: default — terminal.
    return _ClassifyResult.TERMINAL


def _find_validation_error(exc: BaseException) -> pydantic.ValidationError | None:
    """Return the first pydantic.ValidationError found in the exception chain,
    or None if no ValidationError is present.

    Used for logging the unwrapped error (DC-3 diagnosis) and by _classify()
    to detect ValidationErrors at any depth (replaces _has_validation_error_in_chain).

    BFS walks both __cause__ and __context__ chains. The queue holds
    (node, level) tuples — ``level`` tracks the true graph depth (distance
    from the root exception), not the number of nodes visited. The walk stops
    when level >= _MAX_CHAIN_DEPTH, which matches the documented intent
    ("maximum depth _MAX_CHAIN_DEPTH").
    """
    seen: set[int] = set()
    queue: list[tuple[BaseException, int]] = [(exc, 0)]

    while queue:
        current, level = queue.pop(0)
        exc_id = id(current)
        if exc_id in seen:
            continue
        seen.add(exc_id)

        if isinstance(current, pydantic.ValidationError):
            return current

        if level >= _MAX_CHAIN_DEPTH:
            continue

        if current.__cause__ is not None:
            queue.append((current.__cause__, level + 1))
        if current.__context__ is not None:
            queue.append((current.__context__, level + 1))

    return None


def _is_transport_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is in the transport-error allowlist.

    Allowlist (by isinstance check, survives version churn):
      - httpx.TimeoutException (includes ReadTimeout, ConnectTimeout, etc.)
      - httpx.ConnectError
      - httpx.RemoteProtocolError
      - litellm.RateLimitError
      - litellm.APIConnectionError
      - litellm.ServiceUnavailableError
      - pydantic_ai.exceptions.ModelHTTPError with status_code >= 500
      - asyncio.TimeoutError (handled separately in the call loop, not here)

    Note: asyncio.TimeoutError is caught before _classify() in the call() loop,
    so it doesn't reach _classify(). Documented here for completeness.
    """
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            litellm.RateLimitError,
            litellm.APIConnectionError,
            litellm.ServiceUnavailableError,
        ),
    ):
        return True

    # pydantic_ai.exceptions.ModelHTTPError with 5xx status code.
    if isinstance(exc, pydantic_ai.exceptions.ModelHTTPError):
        if hasattr(exc, "status_code") and exc.status_code >= 500:
            return True

    return False


def _is_auth_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a ProviderAuthError (fallback-once-per-chain)."""
    return isinstance(exc, litellm.AuthenticationError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_exhausted_message(
    attempts: list[AttemptRecord],
    fallbacks: list[ModelSpec],
) -> str:
    """Build a diagnostic message for RouterChainExhaustedError.

    Includes fallback_models config and per-attempt trace.
    """
    empty_note = " fallback_models is empty." if not fallbacks else ""
    attempts_str = "; ".join(
        f"{a.model_name}({a.exception_class},{a.duration_ms:.0f}ms)" for a in attempts
    )
    return (
        f"chain_exhausted: all {len(attempts)} model(s) failed.{empty_note} "
        f"fallback_chain={[a.model_name for a in attempts]!r} "
        f"attempts=[{attempts_str}]"
    )


def _make_explicit_agent_factory(
    resolved_keys: dict[str, str],
) -> Callable[[ModelSpec], Agent]:  # type: ignore[type-arg]
    """Return an agent factory that constructs PydanticAI Agents with explicit providers.

    Decision E1: API keys are injected explicitly via resolved_keys (snapshot from
    config load). This factory NEVER reads os.environ. The keys are closed over at
    factory-creation time — this is the DC8 cache-once mechanism.

    Supported providers (explicit PydanticAI model classes):
      - "anthropic": AnthropicModel + AnthropicProvider(api_key=...)
      - "openai": OpenAIModel + OpenAIProvider(api_key=...)

    Unsupported providers raise RouterTerminalError with an actionable message.
    The D6 LiteLLM escape hatch is available by passing a custom agent_factory
    to LLMRouter — the built-in factory does not attempt LiteLLM routing.

    Args:
        resolved_keys: Snapshot dict of provider API keys at config load time.
            Keys: "anthropic", "openai", "custom". Values: resolved key strings.
            Empty string = provider not configured.

    Returns:
        A callable ModelSpec -> Agent.
    """
    # Close over a copy (not a reference) to prevent external mutation.
    _keys = dict(resolved_keys)

    def factory(spec: ModelSpec) -> Agent:  # type: ignore[type-arg]
        if spec.provider == "anthropic":
            api_key = _keys.get("anthropic", "")
            provider = AnthropicProvider(api_key=api_key)
            model = AnthropicModel(spec.name, provider=provider)
            return Agent(model=model, defer_model_check=True)

        if spec.provider == "openai":
            api_key = _keys.get("openai", "")
            provider = OpenAIProvider(api_key=api_key)
            model = OpenAIModel(spec.name, provider=provider)
            return Agent(model=model, defer_model_check=True)

        raise RouterTerminalError(
            f"terminal_error: unsupported provider {spec.provider!r}. "
            f"Built-in factory supports 'anthropic' and 'openai'. "
            f"For other providers, pass a custom agent_factory to LLMRouter (D6 escape hatch). "
            f"model={spec.name!r}"
        )

    return factory


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "AttemptRecord",
    "LLMRouter",
    "ModelSpec",
    "RouterChainExhaustedError",
    "RouterChainTimeoutError",
    "RouterConfig",
    "RouterTerminalError",
    "_make_explicit_agent_factory",
]
