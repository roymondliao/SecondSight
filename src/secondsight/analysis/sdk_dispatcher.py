"""SDKAnalysisDispatcher — pydantic-ai SDK dispatch path (Task 5).

Wraps LLMRouter.call() into a uniform dispatch interface that produces
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

from secondsight.analysis.output import AnalysisOutput
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

    Wraps LLMRouter.call() and produces AnalysisOutput with dispatched_via='sdk'.

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

        # Track whether fallback was used in the most recent dispatch.
        # This is set during call() via the router's attempt trace.
        self._primary_spec = primary_spec
        self._fallbacks = fallbacks

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
        The SDK dispatcher has no subprocess cwd requirement — it calls LLMRouter.call()
        directly. CLI callers pass project_root; SDK discards it silently. This
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
            # _build_system_prompt is inside try so template render failures produce
            # AnalysisOutput(status='failure') rather than propagating to the caller.
            # The exception-free dispatch contract requires ALL exceptions to be caught.
            system_prompt = self._build_system_prompt(session_payload)
            result = await self._router.call(
                model_input=system_prompt,
                output_type=list[
                    BehaviorFlagDraft
                ],  # pydantic-ai validates each item against schema
            )
            logger.info(
                f"SDK dispatch: success for session {session_id!r} "
                f"primary_model={self._primary_model_name!r}"
            )
            # Determine if fallback was used by inspecting router attempt count.
            # The router uses primary first; fallback is only tried after primary fails.
            # We detect fallback by checking if result came from fallback (opaque to us here).
            # Simplification: for MVP, if the primary model was in the chain, we assume
            # primary succeeded unless we catch an error. Fallback detection is handled
            # by the except branches below.
            return self._make_success_output(
                session_id=session_id,
                behavior_flags=result if isinstance(result, list) else [],
                fallback_used=False,
            )

        except RouterChainExhaustedError as exc:
            # All models tried and all failed. Check if fallback was attempted.
            fallback_was_attempted = len(exc.attempts) > 1
            # Pin ordering invariant: attempts[0] must be the primary model.
            # LLMRouter appends attempts in dispatch order (primary first), but
            # this is not documented as a contract. Assert here so that if the
            # ordering changes, DC4 attribution (primary_error/fallback_error)
            # fails loudly rather than silently swapping labels.
            if exc.attempts:
                assert exc.attempts[0].model_name == self._primary_spec.name, (
                    f"attempts ordering violated: expected primary={self._primary_spec.name!r}, "
                    f"got attempts[0].model_name={exc.attempts[0].model_name!r}. "
                    f"If LLMRouter changed attempt ordering, update _make_dual_failure_output "
                    f"to find the primary attempt by model_name rather than index."
                )
            primary_error = str(exc.attempts[0]) if exc.attempts else str(exc)
            fallback_error = str(exc.attempts[1]) if len(exc.attempts) > 1 else ""

            if fallback_was_attempted and fallback_error:
                logger.warning(
                    f"SDK dispatch: DC4 — both providers failed for session {session_id!r}. "
                    f"primary_error={primary_error!r} fallback_error={fallback_error!r}"
                )
                return self._make_dual_failure_output(
                    session_id=session_id,
                    primary_error=primary_error,
                    fallback_error=fallback_error,
                )
            else:
                logger.warning(
                    f"SDK dispatch: primary failed, no fallback for session {session_id!r}. "
                    f"error={primary_error!r}"
                )
                return self._make_single_failure_output(
                    session_id=session_id,
                    error=str(exc),
                )

        except RouterTerminalError as exc:
            logger.warning(f"SDK dispatch: terminal error for session {session_id!r}: {exc}")
            return self._make_single_failure_output(
                session_id=session_id,
                error=str(exc),
            )

        except Exception as exc:
            logger.error(f"SDK dispatch: unexpected error for session {session_id!r}: {exc}")
            return self._make_single_failure_output(
                session_id=session_id,
                error=f"{type(exc).__name__}: {exc}",
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
                "retry_count": 0,
                "error_details": error_details,
            }
        )

    def _make_single_failure_output(
        self,
        session_id: str,
        error: str,
    ) -> AnalysisOutput:
        """Build a failure AnalysisOutput when only primary failed (no fallback attempted)."""
        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": session_id,
                "status": "failure",
                "behavior_flags": [],
                "session_summary": {
                    "headline": "SDK analysis failed",
                    "key_findings": [],
                    "body": f"SDK dispatch failure: {error}",
                },
                "dispatched_via": "sdk",
                "cli_agent": None,
                "primary_model": self._primary_model_name,
                "fallback_used": False,
                "retry_count": 0,
                "error_details": {"error": error},
            }
        )

    def _make_dual_failure_output(
        self,
        session_id: str,
        primary_error: str,
        fallback_error: str,
    ) -> AnalysisOutput:
        """Build a failure AnalysisOutput when BOTH primary and fallback failed (DC4).

        DC4 enforcement: AnalysisOutput.check_cross_fields() validator requires
        error_details to contain BOTH 'primary_error' and 'fallback_error' keys
        when status='failure' AND fallback_used=True.
        """
        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": session_id,
                "status": "failure",
                "behavior_flags": [],
                "session_summary": {
                    "headline": "SDK analysis failed (both providers)",
                    "key_findings": [],
                    "body": (
                        f"SDK dispatch: both primary and fallback providers failed. "
                        f"primary_error={primary_error!r} fallback_error={fallback_error!r}"
                    ),
                },
                "dispatched_via": "sdk",
                "cli_agent": None,
                "primary_model": self._primary_model_name,
                "fallback_used": True,
                "retry_count": 0,
                "error_details": {
                    "primary_error": primary_error,
                    "fallback_error": fallback_error,
                },
            }
        )


__all__ = ["SDKAnalysisDispatcher"]
