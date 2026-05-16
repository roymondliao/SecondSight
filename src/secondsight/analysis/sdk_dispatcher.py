"""SDKAnalysisDispatcher — pydantic-ai SDK dispatch path (Task 5).

Wraps LLMRouter.call_with_metadata() into a uniform dispatch interface that produces
AnalysisOutput instances for Task 6's ProjectAnalysisRuntime.dispatch() to call.

Design choices:
    - pydantic-ai's output_type mechanism handles JSON schema instruction
      injection automatically. The system prompt carries the analysis
      instructions via behavior.jinja2 (option X from task spec).
    - LLMRouter is constructed at __init__ time with resolved_keys (DC8:
      cache-once contract). Key rotation requires server restart.
    - RouterTerminalError at construction time propagates to caller — this
      is a hard failure (no provider keys resolvable). DC7 closed here.
    - Fallback: primary fails with transport error → fallback model tried.
      fallback_used=True in output. On both fail: DC4 enforcement via
      AnalysisOutput.check_cross_fields() validator.
    - behavior.jinja2 is the system prompt template. It references
      flag_definitions_block and segment_json (from behavior.py prompt path)
      but SDKAnalysisDispatcher uses it as system prompt only — pydantic-ai
      enforces the output schema via output_type=BehaviorFlagDraft semantics.
      We use a simplified inline system prompt for now (see scar item).

Death cases closed here:
    DC4: Both primary and fallback fail → AnalysisOutput with
         error_details containing both 'primary_error' and 'fallback_error'.
         Enforced by AnalysisOutput.check_cross_fields() validator.
    DC7: Empty resolved_keys → RouterTerminalError at construction (not dispatch).
    DC8: resolved_keys snapshot at LLMRouter init; env mutations ignored.

Silent failure conditions (see scar report for full list):
    - If LLM returns valid BehaviorFlagDraft but semantically incorrect flags,
      dispatch returns success. No semantic validation is performed here.
    - Provider is "anthropic" or "openai" only. Other providers raise
      RouterTerminalError inside _make_explicit_agent_factory.
    - Session payload size: very large payloads may exceed LLM context limits.
      pydantic-ai will raise UnexpectedModelBehavior → RouterTerminalError.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from secondsight.analysis.output import AnalysisOutput
from secondsight.analysis.output_recovery import (
    ClassifiedFailure,
    EvidenceConfidence,
    ExecutorFailureEvidence,
    FailureClass,
    RecoveryAttempt,
    RecoveryTrace,
    RetryDecision,
    RetryMode,
    build_recovery_error_details,
    classify_output_failure,
    decide_retry,
)
from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType, FLAG_DEFINITIONS
from secondsight.config.schema import AnalysisConfig
from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.model_selection import _infer_provider
from secondsight.sdk.router import (
    LLMRouter,
    RouterChainExhaustedError,
    RouterTerminalError,
)


# ---------------------------------------------------------------------------
# SDKAnalysisDispatcher
# ---------------------------------------------------------------------------


class SDKAnalysisDispatcher:
    """Dispatches analysis via pydantic-ai SDK path.

    Conforms to the AnalysisDispatcher Protocol (analysis/dispatcher.py).
    Task 6's ProjectAnalysisRuntime.dispatch() calls this uniformly alongside
    CLIAnalysisDispatcher — no mode-branching required at the caller.

    Wraps LLMRouter.call_with_metadata() and produces AnalysisOutput with dispatched_via='sdk'.

    Args:
        config: AnalysisConfig. sdk.primary_model and sdk.fallback_model are used.
        resolved_keys: Snapshot of provider API keys at config load time.
            Empty keys → RouterTerminalError at construction. DC7 closed.
        prompt_loader: Optional jinja2 render function. Defaults to Task 3's
            prompts._loader.render. Injected for testing only.
    """

    def __init__(
        self,
        config: AnalysisConfig,
        resolved_keys: dict[str, str],
        prompt_loader: Any = None,
    ) -> None:
        self._config = config
        self._primary_model_name = config.sdk.primary_model
        self._fallback_model_name = config.sdk.fallback_model

        # Determine primary provider from model name heuristic.
        # Convention: claude-* → anthropic; gpt-* / o1-* / o3-* → openai.
        # Custom provider: caller must pass a custom agent_factory (D6 escape hatch).
        primary_provider = _infer_provider(self._primary_model_name)
        primary_spec = ModelSpec(name=self._primary_model_name, provider=primary_provider)

        fallbacks: list[ModelSpec] = []
        if self._fallback_model_name:
            fallback_provider = _infer_provider(self._fallback_model_name)
            fallbacks = [ModelSpec(name=self._fallback_model_name, provider=fallback_provider)]

        # RouterTerminalError raised here if primary provider key is empty (DC7).
        # This is intentional — construction failure is better than dispatch failure.
        self._router = LLMRouter(
            primary=primary_spec,
            fallbacks=fallbacks,
            resolved_keys=resolved_keys,
            per_call_timeout_s=float(config.timeout_seconds),
            chain_total_timeout_s=float(config.timeout_seconds) * 1.5,
        )

        # Used to guard primary/fallback error attribution on exhausted chains.
        self._primary_spec = primary_spec

        if prompt_loader is not None:
            self._render = prompt_loader
        else:
            from secondsight.prompts._loader import render as _default_render

            self._render = _default_render

        logger.debug(
            f"SDKAnalysisDispatcher constructed. "
            f"primary_model={self._primary_model_name!r} "
            f"fallback_model={self._fallback_model_name!r} "
            f"primary_provider={primary_provider!r} "
            f"fallback_count={len(fallbacks)}"
        )

    async def dispatch(
        self,
        session_id: str,
        session_payload: dict[str, Any],
        project_root: Path | None = None,
    ) -> AnalysisOutput:
        """Dispatch one analysis session via the SDK path.

        Conforms to the AnalysisDispatcher Protocol (analysis/dispatcher.py).
        project_root is accepted for Protocol uniformity but IGNORED by the SDK path.
        The SDK dispatcher has no subprocess cwd requirement — it calls
        LLMRouter.call_with_metadata() directly. CLI callers pass project_root; SDK discards it silently. This
        asymmetry is documented at the Protocol level, not hidden here.

        Tries primary model first. On transport failure, falls back if configured.
        On both failing: returns AnalysisOutput(status='failure', fallback_used=True)
        with error_details carrying both errors (DC4).

        Args:
            session_id: ID of the session being analyzed.
            session_payload: The session data dict to include in the prompt.
            project_root: Accepted for Protocol conformance. Not used by SDK dispatch.
                CLIAnalysisDispatcher uses this as subprocess cwd; SDK ignores it.

        Returns:
            AnalysisOutput with dispatched_via='sdk'. Never raises (exception-free
            dispatch contract matches CLIAnalysisDispatcher).
        """
        try:
            system_prompt = self._build_system_prompt(session_payload)
            max_attempts = (
                self._config.retry.output_repair_max_attempts + 1
                if self._config.retry.enabled
                else 1
            )
            recovery_attempts: list[RecoveryAttempt] = []
            current_prompt = system_prompt

            for attempt_number in range(1, max_attempts + 1):
                try:
                    router_result = await self._router.call_with_metadata(
                        model_input=current_prompt,
                        output_type=list[
                            BehaviorFlagDraft
                        ],  # pydantic-ai validates each item against schema
                    )
                except AssertionError:
                    raise
                except Exception as exc:
                    router_context = self._extract_router_failure_context(exc)
                    failure = self._classify_dispatch_failure(exc)
                    decision = decide_retry(
                        failure,
                        attempt_number=attempt_number,
                        max_attempts=max_attempts,
                        feedback_max_chars=self._config.retry.feedback_max_chars,
                    )
                    recovery_attempts.append(
                        RecoveryAttempt(
                            attempt_number=attempt_number,
                            executor="sdk",
                            failure_class=failure.failure_class,
                            reason=failure.reason,
                            error=failure.error,
                            details={
                                **failure.details,
                                **router_context["trace_details"],
                            },
                        )
                    )
                    trace = RecoveryTrace(
                        attempts=list(recovery_attempts),
                        final_decision=decision,
                    )

                    logger.warning(
                        f"SDK dispatch: attempt {attempt_number}/{max_attempts} failed "
                        f"for session {session_id!r}. "
                        f"failure_class={failure.failure_class.value!r} "
                        f"retry_mode={decision.retry_mode.value!r} "
                        f"should_retry={decision.should_retry}"
                    )

                    if decision.should_retry:
                        current_prompt = (
                            self._augment_prompt_with_retry_feedback(
                                system_prompt,
                                decision.retry_feedback,
                            )
                            if decision.retry_mode is RetryMode.OUTPUT_REPAIR
                            else system_prompt
                        )
                        continue

                    return self._make_failure_output(
                        session_id=session_id,
                        failure=failure,
                        decision=decision,
                        trace=trace,
                        retry_count=attempt_number - 1,
                        fallback_used=router_context["fallback_used"],
                        extra_error_details=router_context["error_details"],
                    )

                logger.info(
                    f"SDK dispatch: success for session {session_id!r} "
                    f"primary_model={self._primary_model_name!r} "
                    f"attempt={attempt_number}/{max_attempts} "
                    f"fallback_used={router_result.fallback_used}"
                )
                return self._make_success_output(
                    session_id=session_id,
                    behavior_flags=(
                        router_result.output if isinstance(router_result.output, list) else []
                    ),
                    fallback_used=router_result.fallback_used,
                    retry_count=attempt_number - 1,
                )

            raise RuntimeError("SDK dispatch retry loop exited without returning")

        except AssertionError:
            raise
        except Exception as exc:
            logger.error(f"SDK dispatch: unexpected error for session {session_id!r}: {exc}")
            failure = ClassifiedFailure(
                failure_class=FailureClass.FATAL_EXECUTION_ERROR,
                reason="fatal_execution_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            decision = RetryDecision(
                should_retry=False,
                retry_mode=RetryMode.NONE,
                reason=failure.reason,
                failure_class=failure.failure_class,
                attempt_number=1,
                max_attempts=1,
                next_attempt_number=None,
            )
            trace = RecoveryTrace(
                attempts=[
                    RecoveryAttempt(
                        attempt_number=1,
                        executor="sdk",
                        failure_class=failure.failure_class,
                        reason=failure.reason,
                        error=failure.error,
                        details=dict(failure.details),
                    )
                ],
                final_decision=decision,
            )
            return self._make_failure_output(
                session_id=session_id,
                failure=failure,
                decision=decision,
                trace=trace,
                retry_count=0,
            )

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _build_system_prompt(self, session_payload: dict[str, Any]) -> str:
        """Build the system prompt string for the pydantic-ai agent.

        Uses behavior.jinja2 as the system prompt template (option X).
        pydantic-ai's output_type mechanism handles schema instruction injection
        separately — the template's [Output Format] section is supplementary.

        Template render failures are FATAL — re-raises the original exception.
        The previous inline-fallback approach was removed because it omitted
        flag_definitions_block and analysis_output_schema, causing the LLM to
        produce semantically empty output while reporting status='success'.
        Task 3's StrictUndefined was specifically designed for DC9 protection;
        suppressing it here defeated that purpose. Loud failure is correct:
        the caller's dispatch() catches all exceptions and returns AnalysisOutput
        with status='failure', which is the observable degradation path.

        Raises:
            Exception: Re-raised from self._render() on template render failure.
                dispatch() catches this and produces AnalysisOutput(status='failure').
        """
        flag_defs_lines = [
            f"- {ft.value}: {FLAG_DEFINITIONS[ft]['description']}" for ft in BehaviorFlagType
        ]
        flag_definitions_block = "\n".join(flag_defs_lines)
        schema = BehaviorFlagDraft.model_json_schema()

        return self._render(
            "analysis/behavior",
            context={
                "flag_definitions_block": flag_definitions_block,
                "segment_json": json.dumps(session_payload, indent=2),
                "analysis_output_schema": json.dumps(schema, indent=2),
            },
        )

    def _make_success_output(
        self,
        session_id: str,
        behavior_flags: list[Any],
        fallback_used: bool,
        retry_count: int = 0,
    ) -> AnalysisOutput:
        """Build a success AnalysisOutput for SDK dispatch.

        If any flags fail model_validate(), they are dropped and counted.
        A non-zero dropped_count is populated in error_details["dropped_flags"]
        so callers can detect observable degradation rather than silent content loss.
        """
        # Coerce flags to BehaviorFlagDraft instances (pydantic-ai may return dicts)
        validated_flags: list[BehaviorFlagDraft] = []
        dropped_count: int = 0

        for i, flag in enumerate(behavior_flags):
            try:
                if isinstance(flag, BehaviorFlagDraft):
                    validated_flags.append(flag)
                elif isinstance(flag, dict):
                    validated_flags.append(BehaviorFlagDraft.model_validate(flag))
            except Exception as exc:
                logger.warning(
                    f"_make_success_output: dropping invalid flag at index {i} "
                    f"for session {session_id!r}: {exc!r}"
                )
                dropped_count += 1

        if dropped_count > 0:
            logger.warning(
                f"_make_success_output: {dropped_count} flag(s) dropped due to "
                f"validation failure for session {session_id!r}. "
                f"Total input flags: {len(behavior_flags)}, "
                f"valid flags: {len(validated_flags)}. "
                f"Output reports status='success' with reduced flag count."
            )

        error_details = None
        if dropped_count > 0:
            error_details = {"dropped_flags": dropped_count}

        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": session_id,
                "status": "success",
                "behavior_flags": [f.model_dump() for f in validated_flags],
                "session_summary": {
                    "headline": "SDK analysis complete",
                    "key_findings": [],
                    "body": f"Analyzed via SDK. Flags detected: {len(validated_flags)}.",
                },
                "dispatched_via": "sdk",
                "cli_agent": None,
                "primary_model": self._primary_model_name,
                "fallback_used": fallback_used,
                "retry_count": retry_count,
                "error_details": error_details,
            }
        )

    def _make_failure_output(
        self,
        session_id: str,
        failure: ClassifiedFailure,
        decision: RetryDecision,
        trace: RecoveryTrace,
        retry_count: int,
        fallback_used: bool = False,
        extra_error_details: dict[str, Any] | None = None,
    ) -> AnalysisOutput:
        """Build a failure AnalysisOutput with shared recovery taxonomy details."""
        error_details = build_recovery_error_details(
            reason=decision.reason,
            failure_class=decision.failure_class,
            attempts=len(trace.attempts),
            retry_exhausted=decision.reason == "retry_exhausted",
            retry_mode=decision.retry_mode,
            error=failure.error,
            recovery_trace=trace.to_log_dict(),
            extra_error_details=failure.details,
            additional_error_details=extra_error_details,
        )

        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": session_id,
                "status": "failure",
                "behavior_flags": [],
                "session_summary": {
                    "headline": "SDK analysis failed",
                    "key_findings": [],
                    "body": f"SDK dispatch failure: {failure.error}",
                },
                "dispatched_via": "sdk",
                "cli_agent": None,
                "primary_model": self._primary_model_name,
                "fallback_used": fallback_used,
                "retry_count": retry_count,
                "error_details": error_details,
            }
        )

    def _extract_router_failure_context(self, exc: Exception) -> dict[str, Any]:
        """Preserve SDK router trace evidence without changing shared taxonomy."""

        if not isinstance(exc, (RouterChainExhaustedError, RouterTerminalError)):
            return {
                "fallback_used": False,
                "error_details": {},
                "trace_details": {},
            }

        attempt_classes = [attempt.exception_class for attempt in exc.attempts]
        extra_error_details: dict[str, Any] = {}
        trace_details: dict[str, Any] = {"attempt_classes": attempt_classes}
        fallback_used = len(exc.attempts) > 1

        if fallback_used:
            assert exc.attempts[0].model_name == self._primary_spec.name, (
                f"attempts ordering violated: expected primary={self._primary_spec.name!r}, "
                f"got attempts[0].model_name={exc.attempts[0].model_name!r}. "
                f"If LLMRouter changed attempt ordering, update _extract_router_failure_context "
                f"to find the primary attempt by model_name rather than index."
            )
            extra_error_details["primary_error"] = str(exc.attempts[0])
            extra_error_details["fallback_error"] = str(exc.attempts[1])
            trace_details["primary_error"] = str(exc.attempts[0])
            trace_details["fallback_error"] = str(exc.attempts[1])

        return {
            "fallback_used": fallback_used,
            "error_details": extra_error_details,
            "trace_details": trace_details,
        }

    def _classify_dispatch_failure(self, exc: Exception) -> ClassifiedFailure:
        """Classify wrapped SDK failures without losing validation semantics."""

        validation_error = self._find_validation_error(exc)
        if validation_error is None:
            evidence = self._extract_sdk_failure_evidence(exc)
            failure = (
                classify_output_failure(exc, evidence=evidence)
                if evidence is not None
                else classify_output_failure(exc)
            )
            if (
                failure.failure_class is FailureClass.FATAL_EXECUTION_ERROR
                and type(exc).__name__ not in failure.error
            ):
                return ClassifiedFailure(
                    failure_class=failure.failure_class,
                    reason=failure.reason,
                    error=f"{type(exc).__name__}: {failure.error}",
                    details=dict(failure.details),
                )
            return failure

        failure = classify_output_failure(validation_error)
        if str(exc) == str(validation_error):
            return failure

        return ClassifiedFailure(
            failure_class=failure.failure_class,
            reason=failure.reason,
            error=failure.error,
            details={
                **failure.details,
                "outer_error": str(exc),
            },
        )

    def _extract_sdk_failure_evidence(self, exc: Exception) -> ExecutorFailureEvidence | None:
        """Extract SDK/router-owned evidence before shared classification."""

        if isinstance(exc, (RouterChainExhaustedError, RouterTerminalError)) and exc.attempts:
            attempt_classes = [str(attempt.exception_class) for attempt in exc.attempts]
            failure_class = _classify_sdk_attempt_classes(
                attempt_classes,
                terminal=isinstance(exc, RouterTerminalError),
            )
            return ExecutorFailureEvidence(
                source="sdk_router_attempt_trace",
                executor="sdk",
                failure_class=failure_class,
                reason=failure_class.value,
                message=str(exc),
                raw={"attempt_classes": attempt_classes},
                confidence=EvidenceConfidence.TYPED,
            )

        return None

    def _find_validation_error(self, exc: Exception) -> ValidationError | None:
        """Find the first ValidationError in __cause__/__context__ chains."""

        queue: list[BaseException] = [exc]
        seen: set[int] = set()

        while queue:
            current = queue.pop(0)
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)

            if isinstance(current, ValidationError):
                return current

            cause = getattr(current, "__cause__", None)
            context = getattr(current, "__context__", None)
            if isinstance(cause, BaseException):
                queue.append(cause)
            if isinstance(context, BaseException):
                queue.append(context)

        return None

    def _augment_prompt_with_retry_feedback(
        self,
        original_prompt: str,
        retry_feedback: str,
    ) -> str:
        """Append shared output-repair feedback to the next SDK attempt."""

        if not retry_feedback:
            return original_prompt

        return (
            original_prompt
            + "\n\n[IMPORTANT: Previous attempt failed validation -- fix it]\n"
            + retry_feedback
            + "\n\nReturn ONLY the requested structured output.\n"
        )


def _classify_sdk_attempt_classes(
    attempt_classes: list[str],
    *,
    terminal: bool,
) -> FailureClass:
    last_class = attempt_classes[-1]
    classes = set(attempt_classes)

    if _is_sdk_auth_exception_class(last_class):
        return FailureClass.FATAL_AUTH_OR_CONFIG
    if _is_sdk_timeout_exception_class(last_class):
        return FailureClass.TRANSPORT_TIMEOUT
    if _is_sdk_rate_limit_exception_class(last_class):
        return FailureClass.TRANSPORT_RATE_LIMIT
    if classes and all(_is_sdk_auth_exception_class(name) for name in classes):
        return FailureClass.FATAL_AUTH_OR_CONFIG
    if terminal:
        return FailureClass.FATAL_EXECUTION_ERROR
    return FailureClass.TRANSPORT_API_ERROR


def _is_sdk_timeout_exception_class(name: str) -> bool:
    return name in {"RouterChainTimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"}


def _is_sdk_rate_limit_exception_class(name: str) -> bool:
    return "RateLimit" in name


def _is_sdk_auth_exception_class(name: str) -> bool:
    return name in {"AuthenticationError", "AuthError"} or "Authentication" in name


__all__ = ["SDKAnalysisDispatcher"]
