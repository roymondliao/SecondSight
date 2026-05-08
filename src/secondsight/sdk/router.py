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
import logging
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

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.sdk._specs import ModelSpec

_logger = logging.getLogger(__name__)

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
        per_call_timeout_s: float = 60.0,
        chain_total_timeout_s: float = 90.0,
        agent_factory: Callable[[ModelSpec], Any] | None = None,
    ) -> None:
        self._primary = primary
        self._fallbacks = fallbacks
        self._per_call_timeout_s = per_call_timeout_s
        self._chain_total_timeout_s = chain_total_timeout_s
        self._agent_factory = agent_factory or _default_agent_factory

        if len(fallbacks) == 0:
            _logger.warning(
                "LLMRouter constructed with empty fallback chain "
                "(fallback_models is empty). "
                "A transport error on the primary will raise immediately "
                "with no retry. "
                "primary=%r",
                primary.name,
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
                _logger.warning(
                    "chain_total_timeout_exceeded before attempt %d/%d "
                    "provider=%r model=%r elapsed_s=%.3f budget_s=%.3f",
                    attempt_idx + 1,
                    total_models,
                    spec.provider,
                    spec.name,
                    elapsed,
                    self._chain_total_timeout_s,
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
                _logger.error(
                    "agent_factory raised during construction "
                    "provider=%r model=%r attempt=%d exc_type=%r exc=%s",
                    spec.provider,
                    spec.name,
                    attempt_idx + 1,
                    type(factory_exc).__name__,
                    factory_exc,
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
                usage = result.usage() if callable(getattr(result, "usage", None)) else getattr(result, "usage", None)
                tokens_in = usage.request_tokens if usage is not None else None
                tokens_out = usage.response_tokens if usage is not None else None

                _logger.info(
                    "llm_attempt provider=%r model=%r tokens_in=%s tokens_out=%s "
                    "duration_ms=%.1f attempt=%d total_attempts=%d outcome=success",
                    spec.provider,
                    spec.name,
                    tokens_in,
                    tokens_out,
                    duration_ms,
                    attempt_idx + 1,
                    total_models,
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
                _logger.info(
                    "llm_attempt provider=%r model=%r tokens_in=%s tokens_out=%s "
                    "duration_ms=%.1f attempt=%d total_attempts=%d outcome=fallback_triggered "
                    "reason=per_call_timeout",
                    spec.provider,
                    spec.name,
                    None,
                    None,
                    duration_ms,
                    attempt_idx + 1,
                    total_models,
                )
                # asyncio.TimeoutError is a transport error — continue fallback.

            except Exception as exc:
                duration_ms = (time.monotonic() - call_start) * 1000
                classify_result = _classify(exc, _seen_auth_errors)

                if classify_result == _ClassifyResult.TERMINAL:
                    # Find and log the root ValidationError if present (DC-3 diagnosis).
                    root_validation = _find_validation_error(exc)
                    if root_validation is not None:
                        _logger.warning(
                            "llm_attempt provider=%r model=%r duration_ms=%.1f "
                            "attempt=%d total_attempts=%d outcome=terminal_error "
                            "exc_type=%r reason=ValidationError_in_chain "
                            "validation_error=%s",
                            spec.provider,
                            spec.name,
                            duration_ms,
                            attempt_idx + 1,
                            total_models,
                            type(exc).__name__,
                            root_validation,
                        )
                        raise RouterTerminalError(
                            f"terminal_error: ValidationError found in exception chain "
                            f"(DC-3 cost-leak prevention, not retrying). "
                            f"outer_exc={type(exc).__name__!r} "
                            f"validation_error={root_validation!s} "
                            f"model={spec.name!r} attempt={attempt_idx + 1}/{total_models}"
                        ) from exc
                    else:
                        _logger.warning(
                            "llm_attempt provider=%r model=%r duration_ms=%.1f "
                            "attempt=%d total_attempts=%d outcome=terminal_error "
                            "exc_type=%r exc=%s",
                            spec.provider,
                            spec.name,
                            duration_ms,
                            attempt_idx + 1,
                            total_models,
                            type(exc).__name__,
                            exc,
                        )
                        raise RouterTerminalError(
                            f"terminal_error: {type(exc).__name__}: {exc}. "
                            f"model={spec.name!r} attempt={attempt_idx + 1}/{total_models}"
                        ) from exc

                elif classify_result == _ClassifyResult.AUTH_ONCE:
                    auth_key = (spec.provider, type(exc).__name__)
                    if auth_key in _seen_auth_errors:
                        # Second auth error in this chain → terminal.
                        _logger.warning(
                            "llm_attempt provider=%r model=%r duration_ms=%.1f "
                            "attempt=%d total_attempts=%d outcome=terminal_error "
                            "reason=repeated_auth_error exc_type=%r",
                            spec.provider,
                            spec.name,
                            duration_ms,
                            attempt_idx + 1,
                            total_models,
                            type(exc).__name__,
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
                        _logger.info(
                            "llm_attempt provider=%r model=%r tokens_in=%s tokens_out=%s "
                            "duration_ms=%.1f attempt=%d total_attempts=%d outcome=fallback_triggered "
                            "reason=auth_error_once",
                            spec.provider,
                            spec.name,
                            None,
                            None,
                            duration_ms,
                            attempt_idx + 1,
                            total_models,
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
                    _logger.info(
                        "llm_attempt provider=%r model=%r tokens_in=%s tokens_out=%s "
                        "duration_ms=%.1f attempt=%d total_attempts=%d outcome=fallback_triggered "
                        "reason=%r",
                        spec.provider,
                        spec.name,
                        None,
                        None,
                        duration_ms,
                        attempt_idx + 1,
                        total_models,
                        type(exc).__name__,
                    )

            # Check chain total budget AFTER this attempt.
            elapsed_after = time.monotonic() - chain_start
            if elapsed_after >= self._chain_total_timeout_s and attempt_idx < len(chain) - 1:
                _logger.warning(
                    "chain_total_timeout_exceeded after attempt %d/%d "
                    "provider=%r model=%r elapsed_s=%.3f budget_s=%.3f",
                    attempt_idx + 1,
                    total_models,
                    spec.provider,
                    spec.name,
                    elapsed_after,
                    self._chain_total_timeout_s,
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
    if isinstance(exc, (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
        litellm.RateLimitError,
        litellm.APIConnectionError,
        litellm.ServiceUnavailableError,
    )):
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
        f"{a.model_name}({a.exception_class},{a.duration_ms:.0f}ms)"
        for a in attempts
    )
    return (
        f"chain_exhausted: all {len(attempts)} model(s) failed.{empty_note} "
        f"fallback_chain={[a.model_name for a in attempts]!r} "
        f"attempts=[{attempts_str}]"
    )


def _default_agent_factory(spec: ModelSpec) -> Agent:  # type: ignore[type-arg]
    """Construct a PydanticAI Agent for ``spec``.

    Uses the model name directly as the model identifier for PydanticAI.
    For non-OpenAI-compatible providers, the caller should pass a custom
    agent_factory that sets up LiteLLM or another provider (D6 escape hatch).

    This implementation assumes:
      - The PydanticAI model name convention: "{provider}:{model_name}" or
        just the model name for OpenAI-compatible providers.
      - API keys are picked up from environment variables (standard convention).
    """
    model_id = f"{spec.provider}:{spec.name}" if spec.provider != "openai" else spec.name
    return Agent(model_id)


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
]
