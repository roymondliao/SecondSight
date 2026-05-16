"""Tests for SDKAnalysisDispatcher — death tests first, then happy path.

Death cases:
  DC4: Both primary and fallback fail → AnalysisOutput.error_details has BOTH errors
  DC7: Empty resolved_keys propagated → RouterTerminalError, not silent failure
  Happy path: primary succeeds → AnalysisOutput(dispatched_via='sdk', status='success')
  Degradation: primary fails → fallback succeeds → fallback_used=True
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest
from pydantic import ValidationError

from secondsight.analysis.output import AnalysisOutput
from secondsight.analysis.schemas import BehaviorFlagDraft
from secondsight.config.schema import (
    AnalysisConfig,
    AnalysisCLIConfig,
    AnalysisRetryConfig,
    AnalysisSDKConfig,
)
from secondsight.state import SecondSightState
from secondsight.sdk.router import (
    AttemptRecord,
    RouterCallResult,
    RouterChainExhaustedError,
    RouterTerminalError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Test-only model sentinels. We can't use BUILTIN_SDK_PRIMARY_MODEL /
# BUILTIN_SDK_FALLBACK_MODEL here because those constants are intentionally
# empty strings (post-2026-05-15 — see test_schema_v2.py for the rationale).
# The dispatcher needs a model name that _infer_provider() can resolve, so
# we pick concrete strings whose prefix maps to a known provider.
_TEST_PRIMARY_MODEL = "claude-haiku-4-5-20251001"
_TEST_FALLBACK_MODEL = "gpt-4o-mini"


def _make_sdk_config(
    primary_model: str = _TEST_PRIMARY_MODEL,
    fallback_model: str = _TEST_FALLBACK_MODEL,
    timeout_seconds: int = 30,
    retry_enabled: bool = True,
    output_repair_max_attempts: int = 2,
    feedback_max_chars: int = 1200,
) -> AnalysisConfig:
    return AnalysisConfig(
        timeout_seconds=timeout_seconds,
        cli=AnalysisCLIConfig(),
        sdk=AnalysisSDKConfig(
            primary_model=primary_model,
            fallback_model=fallback_model,
        ),
        retry=AnalysisRetryConfig(
            enabled=retry_enabled,
            output_repair_max_attempts=output_repair_max_attempts,
            feedback_max_chars=feedback_max_chars,
        ),
    )


def _make_valid_output_dict(
    session_id: str = "sess-001",
    status: str = "success",
    primary_model: str = _TEST_PRIMARY_MODEL,
    fallback_used: bool = False,
    error_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    d = {
        "schema_version": "1.0",
        "session_id": session_id,
        "status": status,
        "behavior_flags": [],
        "session_summary": {
            "headline": "All good",
            "key_findings": [],
            "body": "No issues detected.",
        },
        "dispatched_via": "sdk",
        "cli_agent": None,
        "primary_model": primary_model,
        "fallback_used": fallback_used,
        "retry_count": 0,
        "error_details": error_details,
    }
    return d


class _FakeRouter:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def call(self, *, model_input: Any, output_type: type[Any]) -> Any:
        result = await self.call_with_metadata(model_input=model_input, output_type=output_type)
        return result.output

    async def call_with_metadata(
        self,
        *,
        model_input: Any,
        output_type: type[Any],
    ) -> RouterCallResult:
        self.calls.append({"model_input": model_input, "output_type": output_type})
        response = self._responses.pop(0)
        if callable(response):
            return response()
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, RouterCallResult):
            return response
        return RouterCallResult(
            output=response,
            fallback_used=False,
        )


def _make_router_terminal_error_with_cause(cause: Exception) -> Callable[[], None]:
    def _raise() -> None:
        raise RouterTerminalError(f"terminal_error: {type(cause).__name__}: {cause}") from cause

    return _raise


def _make_schema_validation_router_error() -> Callable[[], None]:
    try:
        BehaviorFlagDraft.model_validate(
            {
                "flag_type": "NOT_A_REAL_FLAG",
                "event_ids": ["evt-1"],
                "reason": "bad flag",
                "confidence": "high",
            }
        )
    except ValidationError as exc:
        return _make_router_terminal_error_with_cause(exc)

    raise AssertionError("Expected invalid BehaviorFlagDraft payload to raise ValidationError")


def _make_cli_proc_mock(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    mock_proc.kill = MagicMock()
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=None)
    return mock_proc


# ---------------------------------------------------------------------------
# Phase 2 death tests: shared recovery adoption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_output_validation_failure_enters_output_repair_retry():
    """Structured output validation failure must use shared output-repair retry."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=1, feedback_max_chars=400),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    fake_router = _FakeRouter([_make_schema_validation_router_error(), []])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-schema-retry",
        session_payload={"events": []},
    )

    assert output.status == "success"
    assert output.retry_count == 1
    assert len(fake_router.calls) == 2, "Schema mismatch must trigger one shared retry"
    assert (
        "Previous output did not match the required JSON schema."
        in fake_router.calls[1]["model_input"]
    )
    assert "Fix the schema issues below" in fake_router.calls[1]["model_input"]


@pytest.mark.asyncio
async def test_provider_auth_config_failure_is_no_retry():
    """Provider auth/config failure must fail fast with shared fatal classification."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=2),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    auth_error = litellm.AuthenticationError(
        message="bad api key",
        llm_provider="openai",
        model=_TEST_PRIMARY_MODEL,
    )
    fake_router = _FakeRouter([_make_router_terminal_error_with_cause(auth_error)])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-auth-fail",
        session_payload={"events": []},
    )

    assert output.status == "failure"
    assert output.retry_count == 0
    assert len(fake_router.calls) == 1, "Fatal auth/config failures must not retry"
    assert output.error_details is not None
    assert output.error_details["reason"] == "fatal_auth_or_config"
    assert output.error_details["failure_class"] == "fatal_auth_or_config"
    assert output.error_details["retry_exhausted"] is False


@pytest.mark.asyncio
async def test_transport_timeout_uses_transport_classification_not_schema_feedback():
    """Transport timeout must be classified without spending output-repair retry budget."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=1),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    timeout_error = RouterChainExhaustedError(
        "chain_exhausted after transport timeout",
        attempts=[
            AttemptRecord(
                model_name=_TEST_PRIMARY_MODEL,
                exception_class="TimeoutError",
                duration_ms=125.0,
            ),
            AttemptRecord(
                model_name=_TEST_FALLBACK_MODEL,
                exception_class="TimeoutError",
                duration_ms=120.0,
            ),
        ],
    )
    fake_router = _FakeRouter([timeout_error])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-timeout-retry",
        session_payload={"events": []},
    )

    assert output.status == "failure"
    assert output.retry_count == 0
    assert len(fake_router.calls) == 1
    assert output.error_details is not None
    assert output.error_details["reason"] == "transport_timeout"
    assert output.error_details["failure_class"] == "transport_timeout"
    assert output.error_details["retry_exhausted"] is False
    assert output.error_details["retry_mode"] == "transport"


@pytest.mark.asyncio
async def test_schema_retry_exhaustion_reports_same_shared_taxonomy_in_cli_and_sdk(tmp_path):
    """Same schema-mismatch exhaustion must produce the same shared observability fields."""
    from secondsight.analysis.cli_dispatcher import CLIAnalysisDispatcher
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    retry_config = AnalysisRetryConfig(output_repair_max_attempts=0, feedback_max_chars=400)
    cli_dispatcher = CLIAnalysisDispatcher(
        config=AnalysisConfig(
            timeout_seconds=30,
            cli=AnalysisCLIConfig(),
            retry=retry_config,
        ),
        state=SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T00:00:00+00:00",
            secondsight_version="0.1.0",
        ),
    )
    sdk_dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=0, feedback_max_chars=400),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    bad_cli_json = json.dumps({"status": "ok"})
    fake_router = _FakeRouter([_make_schema_validation_router_error()])
    cast(Any, sdk_dispatcher)._router = fake_router
    cast(Any, sdk_dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    with patch(
        "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
        return_value=_make_cli_proc_mock(stdout=bad_cli_json, stderr="cli-schema-stderr"),
    ):
        cli_output = await cli_dispatcher.dispatch(
            session_id="sess-cross-mode-schema",
            project_root=tmp_path,
            session_payload={"events": []},
        )

    sdk_output = await sdk_dispatcher.dispatch(
        session_id="sess-cross-mode-schema",
        session_payload={"events": []},
    )

    assert cli_output.error_details is not None
    assert sdk_output.error_details is not None
    for field, expected in {
        "reason": "retry_exhausted",
        "failure_class": "schema_mismatch",
        "attempts": 1,
        "retry_exhausted": True,
    }.items():
        assert cli_output.error_details[field] == expected
        assert sdk_output.error_details[field] == expected


# ---------------------------------------------------------------------------
# Phase 2 unit tests: shared failure details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_retry_exhaustion_reports_shared_error_details():
    """Exhausted schema retry must surface shared reason/class/attempt accounting."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=0, feedback_max_chars=400),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    fake_router = _FakeRouter([_make_schema_validation_router_error()])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-schema-exhausted",
        session_payload={"events": []},
    )

    assert output.status == "failure"
    assert output.error_details is not None
    assert output.error_details["reason"] == "retry_exhausted"
    assert output.error_details["failure_class"] == "schema_mismatch"
    assert output.error_details["attempts"] == 1
    assert output.error_details["retry_exhausted"] is True


@pytest.mark.asyncio
async def test_transport_failure_preserves_sdk_attempt_trace_details():
    """Router attempt classes must survive shared classification into SDK error_details."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=0),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    timeout_error = RouterChainExhaustedError(
        "chain_exhausted after transport timeout",
        attempts=[
            AttemptRecord(
                model_name=_TEST_PRIMARY_MODEL,
                exception_class="TimeoutError",
                duration_ms=125.0,
            ),
            AttemptRecord(
                model_name=_TEST_FALLBACK_MODEL,
                exception_class="TimeoutError",
                duration_ms=120.0,
            ),
        ],
    )
    fake_router = _FakeRouter([timeout_error])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-timeout-failure",
        session_payload={"events": []},
    )

    assert output.status == "failure"
    assert output.error_details is not None
    assert output.error_details["failure_class"] == "transport_timeout"
    assert output.error_details["reason"] == "transport_timeout"
    assert output.error_details["attempts"] == 1
    assert output.error_details["attempt_classes"] == ["TimeoutError", "TimeoutError"]
    assert output.error_details["evidence_source"] == "sdk_router_attempt_trace"
    assert output.error_details["evidence_confidence"] == "typed"
    assert output.error_details["evidence_executor"] == "sdk"
    assert output.error_details["retry_exhausted"] is False


def test_sdk_attempt_evidence_beats_misleading_outer_message() -> None:
    """Router attempt records must drive SDK classification before message text."""
    from secondsight.analysis.output_recovery import FailureClass
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)
    exc = RouterChainExhaustedError(
        "outer message says authentication failed, but attempt class is RateLimitError",
        attempts=[
            AttemptRecord(
                model_name=_TEST_PRIMARY_MODEL,
                exception_class="RateLimitError",
                duration_ms=125.0,
            )
        ],
    )

    failure = dispatcher._classify_dispatch_failure(exc)

    assert failure.failure_class is FailureClass.TRANSPORT_RATE_LIMIT
    assert failure.reason == "transport_rate_limit"
    assert failure.details["evidence_source"] == "sdk_router_attempt_trace"
    assert failure.details["evidence_confidence"] == "typed"
    assert failure.details["evidence_executor"] == "sdk"
    assert failure.details["attempt_classes"] == ["RateLimitError"]


def test_sdk_controlled_config_error_without_attempts_uses_sdk_evidence() -> None:
    from secondsight.analysis.output_recovery import FailureClass
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)
    failure = dispatcher._classify_dispatch_failure(
        RouterTerminalError(
            "no provider keys resolvable: primary provider 'openai' has empty key in resolved_keys"
        )
    )

    assert failure.failure_class is FailureClass.FATAL_AUTH_OR_CONFIG
    assert failure.reason == "fatal_auth_or_config"
    assert failure.details["evidence_source"] == "sdk_controlled_config"
    assert failure.details["evidence_confidence"] == "derived"
    assert failure.details["evidence_executor"] == "sdk"


@pytest.mark.asyncio
async def test_fallback_terminal_failure_marks_fallback_used_and_preserves_both_errors():
    """If fallback is attempted then terminally fails, failure output must say so."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=0),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    terminal_error = RouterTerminalError(
        "terminal_error: UnexpectedModelBehavior on fallback",
        attempts=[
            AttemptRecord(
                model_name=_TEST_PRIMARY_MODEL,
                exception_class="RateLimitError",
                duration_ms=125.0,
            ),
            AttemptRecord(
                model_name=_TEST_FALLBACK_MODEL,
                exception_class="UnexpectedModelBehavior",
                duration_ms=120.0,
            ),
        ],
    )
    fake_router = _FakeRouter([terminal_error])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-fallback-terminal-failure",
        session_payload={"events": []},
    )

    assert output.status == "failure"
    assert output.fallback_used is True
    assert output.error_details is not None
    assert output.error_details["failure_class"] == "fatal_execution_error"
    assert output.error_details["attempt_classes"] == [
        "RateLimitError",
        "UnexpectedModelBehavior",
    ]
    assert output.error_details["evidence_source"] == "sdk_router_attempt_trace"
    assert output.error_details["evidence_confidence"] == "typed"
    assert output.error_details["evidence_executor"] == "sdk"
    assert "primary_error" in output.error_details
    assert "fallback_error" in output.error_details


def test_sdk_failure_output_namespaces_colliding_raw_error_details():
    """SDK raw evidence must not overwrite shared observability fields."""
    from secondsight.analysis.output_recovery import (
        ClassifiedFailure,
        FailureClass,
        RecoveryAttempt,
        RecoveryTrace,
        RetryDecision,
        RetryMode,
    )
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)
    cast(Any, dispatcher)._primary_model_name = _TEST_PRIMARY_MODEL
    failure = ClassifiedFailure(
        failure_class=FailureClass.TRANSPORT_TIMEOUT,
        reason="transport_timeout",
        error="provider timed out",
        details={"reason": "raw reason", "error": "raw provider error", "request_id": "req-123"},
    )
    decision = RetryDecision(
        should_retry=False,
        retry_mode=RetryMode.TRANSPORT,
        reason="transport_timeout",
        failure_class=FailureClass.TRANSPORT_TIMEOUT,
        attempt_number=1,
        max_attempts=3,
        next_attempt_number=None,
    )
    trace = RecoveryTrace(
        attempts=[
            RecoveryAttempt(
                attempt_number=1,
                executor="sdk",
                failure_class=FailureClass.TRANSPORT_TIMEOUT,
                reason="transport_timeout",
                error="provider timed out",
            )
        ],
        final_decision=decision,
    )

    output = dispatcher._make_failure_output(
        session_id="sess-raw-collision",
        failure=failure,
        decision=decision,
        trace=trace,
        retry_count=0,
        extra_error_details={
            "failure_class": "raw class",
            "attempts": 99,
            "attempt_classes": ["TimeoutError"],
        },
    )

    assert output.error_details is not None
    assert output.error_details["reason"] == "transport_timeout"
    assert output.error_details["failure_class"] == "transport_timeout"
    assert output.error_details["error"] == "provider timed out"
    assert output.error_details["attempts"] == 1
    assert output.error_details["request_id"] == "req-123"
    assert output.error_details["attempt_classes"] == ["TimeoutError"]
    assert output.error_details["raw_error_details"] == {
        "reason": "raw reason",
        "error": "raw provider error",
        "failure_class": "raw class",
        "attempts": 99,
    }


def test_sdk_failure_output_redacts_secret_from_session_summary_body():
    from secondsight.analysis.output_recovery import (
        ClassifiedFailure,
        FailureClass,
        RecoveryAttempt,
        RecoveryTrace,
        RetryDecision,
        RetryMode,
    )
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)
    cast(Any, dispatcher)._primary_model_name = _TEST_PRIMARY_MODEL
    failure = ClassifiedFailure(
        failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
        reason="fatal_auth_or_config",
        error="provider rejected api_key=sk-test-secret-1234567890",
    )
    decision = RetryDecision(
        should_retry=False,
        retry_mode=RetryMode.NONE,
        reason="fatal_auth_or_config",
        failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
        attempt_number=1,
        max_attempts=1,
        next_attempt_number=None,
    )
    trace = RecoveryTrace(
        attempts=[
            RecoveryAttempt(
                attempt_number=1,
                executor="sdk",
                failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
                reason="fatal_auth_or_config",
                error=failure.error,
            )
        ],
        final_decision=decision,
    )

    output = dispatcher._make_failure_output(
        session_id="sess-summary-secret",
        failure=failure,
        decision=decision,
        trace=trace,
        retry_count=0,
    )

    assert "sk-test-secret-1234567890" not in output.session_summary.body
    assert "[REDACTED]" in output.session_summary.body


# ---------------------------------------------------------------------------
# DC4 death tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dc4_both_providers_fail_error_details_has_both_errors():
    """DC4: primary AND fallback both fail → error_details with primary_error + fallback_error.

    This is the cross-field invariant enforced by AnalysisOutput.check_cross_fields().
    The dispatcher MUST populate both keys or pydantic validation will reject the output.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    resolved_keys = {"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""}

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=0),
        resolved_keys=resolved_keys,
    )
    chain_error = RouterChainExhaustedError(
        "both providers failed",
        attempts=[
            AttemptRecord(
                model_name=_TEST_PRIMARY_MODEL,
                exception_class="TimeoutError",
                duration_ms=100.0,
            ),
            AttemptRecord(
                model_name=_TEST_FALLBACK_MODEL,
                exception_class="RateLimitError",
                duration_ms=125.0,
            ),
        ],
    )
    fake_router = _FakeRouter([chain_error])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-dc4",
        session_payload={"events": []},
    )

    assert output.status == "failure"
    assert output.fallback_used is True
    assert output.error_details is not None
    assert "primary_error" in output.error_details, (
        "DC4: error_details must have 'primary_error' key"
    )
    assert "fallback_error" in output.error_details, (
        "DC4: error_details must have 'fallback_error' key"
    )


@pytest.mark.asyncio
async def test_dispatcher_contract_validation_error_does_not_enter_output_repair_retry():
    """Dispatcher-built AnalysisOutput validation errors are dispatcher bugs, not model output."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher(
        config=_make_sdk_config(output_repair_max_attempts=2),
        resolved_keys={"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""},
    )
    fake_router = _FakeRouter([[]])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    def _broken_success_output(*args: Any, **kwargs: Any) -> AnalysisOutput:
        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": "bad-output",
                "status": "success",
                "behavior_flags": [],
                "dispatched_via": "sdk",
                "primary_model": _TEST_PRIMARY_MODEL,
            }
        )

    cast(Any, dispatcher)._make_success_output = _broken_success_output

    output = await dispatcher.dispatch(
        session_id="sess-dispatcher-contract-bug",
        session_payload={"events": []},
    )

    assert len(fake_router.calls) == 1, "Dispatcher output bugs must not trigger model retry"
    assert output.status == "failure"
    assert output.error_details is not None
    assert output.error_details["failure_class"] == "fatal_execution_error"
    assert output.error_details["retry_exhausted"] is False


@pytest.mark.asyncio
async def test_dc4_missing_fallback_error_key_fails_validation():
    """DC4: constructing AnalysisOutput with fallback_used=True but missing fallback_error
    must raise pydantic ValidationError.

    This verifies the Task 2 validator enforces DC4 — not just our dispatcher.
    """
    with pytest.raises(ValidationError) as exc_info:
        AnalysisOutput.model_validate(
            _make_valid_output_dict(
                status="failure",
                fallback_used=True,
                error_details={"primary_error": "oops"},
                # fallback_error is intentionally missing
            )
        )

    errors_str = str(exc_info.value)
    assert "fallback_error" in errors_str or "DC4" in errors_str, (
        f"Validation error should mention 'fallback_error' or 'DC4', got: {errors_str}"
    )


@pytest.mark.asyncio
async def test_dc4_missing_primary_error_key_fails_validation():
    """DC4: error_details with fallback_used=True but missing primary_error is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        AnalysisOutput.model_validate(
            _make_valid_output_dict(
                status="failure",
                fallback_used=True,
                error_details={"fallback_error": "also failed"},
                # primary_error is intentionally missing
            )
        )

    errors_str = str(exc_info.value)
    assert "primary_error" in errors_str or "DC4" in errors_str


# ---------------------------------------------------------------------------
# DC7 via dispatcher construction
# ---------------------------------------------------------------------------


def test_dc7_empty_resolved_keys_raises_at_dispatcher_construction():
    """DC7: SDKAnalysisDispatcher with all-empty resolved_keys raises RouterTerminalError
    at construction time (not at dispatch time).
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    config = _make_sdk_config()
    empty_keys: dict[str, str] = {"anthropic": "", "openai": "", "custom": ""}

    with pytest.raises(RouterTerminalError):
        SDKAnalysisDispatcher(
            config=config,
            resolved_keys=empty_keys,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_primary_succeeds():
    """Happy path: primary model succeeds → AnalysisOutput with dispatched_via='sdk',
    status='success', fallback_used=False.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    config = _make_sdk_config()
    resolved_keys = {"anthropic": "sk-valid-for-test", "openai": "sk-openai", "custom": ""}

    dispatcher = SDKAnalysisDispatcher(config=config, resolved_keys=resolved_keys)
    fake_router = _FakeRouter([RouterCallResult(output=[], fallback_used=False)])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-happy",
        session_payload={"events": []},
    )

    assert output.status == "success"
    assert output.dispatched_via == "sdk"
    assert output.fallback_used is False
    assert output.primary_model is not None
    assert output.cli_agent is None


@pytest.mark.asyncio
async def test_fallback_engaged_when_primary_fails():
    """Degradation: primary fails (transport error) → fallback engaged.

    When fallback succeeds: fallback_used=True, status='success'.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    config = _make_sdk_config()
    resolved_keys = {"anthropic": "sk-ant", "openai": "sk-openai", "custom": ""}

    dispatcher = SDKAnalysisDispatcher(config=config, resolved_keys=resolved_keys)
    fake_router = _FakeRouter([RouterCallResult(output=[], fallback_used=True)])
    cast(Any, dispatcher)._router = fake_router
    cast(Any, dispatcher)._build_system_prompt = lambda session_payload: "BASE PROMPT"

    output = await dispatcher.dispatch(
        session_id="sess-fallback",
        session_payload={"events": []},
    )

    assert output.status == "success"
    assert output.fallback_used is True
    assert output.dispatched_via == "sdk"


# ---------------------------------------------------------------------------
# SDKAnalysisDispatcher contract tests
# ---------------------------------------------------------------------------


def test_dispatcher_class_exists_and_has_correct_interface():
    """SDKAnalysisDispatcher must have correct __init__ and dispatch signatures."""
    import inspect
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    sig = inspect.signature(SDKAnalysisDispatcher.__init__)
    params = sig.parameters

    assert "config" in params, "SDKAnalysisDispatcher.__init__ must accept 'config'"
    assert "resolved_keys" in params, "SDKAnalysisDispatcher.__init__ must accept 'resolved_keys'"

    dispatch_sig = inspect.signature(SDKAnalysisDispatcher.dispatch)
    dispatch_params = dispatch_sig.parameters
    assert "session_id" in dispatch_params
    assert "session_payload" in dispatch_params


def test_dispatcher_output_type_is_analysis_output():
    """dispatch() must be annotated to return AnalysisOutput (or coroutine thereof)."""
    import inspect
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    sig = inspect.signature(SDKAnalysisDispatcher.dispatch)
    return_annotation = sig.return_annotation
    # Either AnalysisOutput or Coroutine[..., AnalysisOutput] is acceptable
    annotation_str = str(return_annotation)
    assert "AnalysisOutput" in annotation_str, (
        f"dispatch() return annotation should reference AnalysisOutput, got: {annotation_str!r}"
    )


# ---------------------------------------------------------------------------
# Death test: Fix 1 — dropped flag accounting in _make_success_output
# ---------------------------------------------------------------------------


def test_make_success_output_dropped_flag_is_counted_and_logged(caplog):
    """DT Fix 1: when pydantic-ai returns 3 flags but 1 has invalid flag_type,
    output has 2 valid flags, error_details['dropped_flags'] == 1, and a WARN
    log was emitted.

    Silent failure mode: without this test, the exception handler could silently
    swallow the drop and report status='success' with reduced flag count and
    no observable signal.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher
    from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)
    dispatcher._primary_model_name = _TEST_PRIMARY_MODEL

    # 3 flags: 2 valid BehaviorFlagDraft, 1 dict with invalid flag_type
    valid_flag_1 = BehaviorFlagDraft(
        flag_type=BehaviorFlagType.UNNECESSARY_READ,
        event_ids=["evt-1"],
        reason="Read was not needed",
        confidence="high",
    )
    valid_flag_2 = BehaviorFlagDraft(
        flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
        event_ids=["evt-2"],
        reason="Explored same path twice",
        confidence="medium",
    )
    invalid_flag_dict = {
        "flag_type": "NOT_A_VALID_FLAG_TYPE_XXXX",
        "event_ids": ["evt-3"],
        "reason": "Bad flag",
        "confidence": "high",
    }

    output = dispatcher._make_success_output(
        session_id="sess-test",
        behavior_flags=[valid_flag_1, invalid_flag_dict, valid_flag_2],
        fallback_used=False,
    )

    # 2 valid flags must survive
    assert len(output.behavior_flags) == 2, (
        f"Expected 2 valid flags, got {len(output.behavior_flags)}. "
        f"The invalid flag dict must be dropped, not silently included or crash."
    )

    # error_details must have dropped_flags count
    assert output.error_details is not None, (
        "error_details must be non-None when flags were dropped (Fix 1)"
    )
    assert output.error_details.get("dropped_flags") == 1, (
        f"error_details['dropped_flags'] must be 1, got: {output.error_details}"
    )

    # status must still be success (degraded, not failed)
    assert output.status == "success", (
        "status must remain 'success' even with dropped flags — "
        "this is observable degradation, not a dispatch failure"
    )

    # WARN log must have been emitted (caplog captures loguru via conftest bridge)
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("dropping invalid flag" in r.message for r in warning_records), (
        "A WARNING log must be emitted for each dropped flag. "
        f"Got warning records: {[r.message for r in warning_records]}"
    )


def test_make_success_output_no_drop_gives_no_error_details():
    """Unit test: when all flags are valid, error_details is None (no false positive)."""
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher
    from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)
    dispatcher._primary_model_name = _TEST_PRIMARY_MODEL

    valid_flag = BehaviorFlagDraft(
        flag_type=BehaviorFlagType.UNNECESSARY_READ,
        event_ids=["evt-1"],
        reason="Read was unnecessary",
        confidence="low",
    )

    output = dispatcher._make_success_output(
        session_id="sess-ok",
        behavior_flags=[valid_flag],
        fallback_used=False,
    )

    assert output.status == "success"
    assert len(output.behavior_flags) == 1
    assert output.error_details is None, "error_details must be None when no flags were dropped"


# ---------------------------------------------------------------------------
# Death test: Fix A — Protocol conformance: unified dispatch() signature
# ---------------------------------------------------------------------------


def test_dispatch_signature_accepts_project_root_kwarg():
    """DT Fix A: dispatch() must accept project_root as optional kwarg (Protocol conformance).

    The AnalysisDispatcher Protocol requires both CLI and SDK dispatchers to accept
    project_root: Path | None = None. Without this, Task 6's ProjectAnalysisRuntime
    would be forced to branch on dispatcher type — the mode-awareness-leak pattern.
    """
    import inspect
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    sig = inspect.signature(SDKAnalysisDispatcher.dispatch)
    params = sig.parameters

    assert "project_root" in params, (
        "SDKAnalysisDispatcher.dispatch() must accept 'project_root' kwarg "
        "for AnalysisDispatcher Protocol conformance (Fix A)."
    )
    param = params["project_root"]
    assert param.default is None, (
        f"project_root must default to None (got default={param.default!r}). "
        f"SDK ignores project_root; it defaults to None for callers that don't need it."
    )


def test_cli_dispatch_signature_accepts_project_root_as_kwarg():
    """DT Fix A: CLIAnalysisDispatcher.dispatch() must accept project_root as kwarg
    (was positional in Task 4). Protocol conformance requires optional default=None.
    """
    import inspect
    from secondsight.analysis.cli_dispatcher import CLIAnalysisDispatcher

    sig = inspect.signature(CLIAnalysisDispatcher.dispatch)
    params = sig.parameters

    assert "project_root" in params, (
        "CLIAnalysisDispatcher.dispatch() must accept 'project_root' parameter "
        "for AnalysisDispatcher Protocol conformance (Fix A)."
    )
    param = params["project_root"]
    assert param.default is None, (
        f"project_root must default to None (got default={param.default!r}). "
        f"CLI raises ValueError if None at entry; the default enables the Protocol shape."
    )


@pytest.mark.asyncio
async def test_cli_dispatch_raises_value_error_when_project_root_is_none():
    """DT Fix A: CLIAnalysisDispatcher.dispatch() must raise ValueError when project_root=None.

    CLI genuinely needs project_root for subprocess cwd. Passing None must fail
    explicitly at entry, not silently at _run_with_retry deep in the stack.
    """
    from secondsight.analysis.cli_dispatcher import CLIAnalysisDispatcher

    from secondsight.config.schema import (
        AnalysisConfig,
        AnalysisCLIConfig,
        AnalysisSDKConfig,
    )

    config = AnalysisConfig(
        timeout_seconds=30,
        cli=AnalysisCLIConfig(default_agent="claude_code"),
        sdk=AnalysisSDKConfig(),
    )
    dispatcher = CLIAnalysisDispatcher(config=config, state=None)

    with pytest.raises(ValueError, match="project_root"):
        await dispatcher.dispatch(
            session_id="sess-test",
            session_payload={"events": []},
            project_root=None,
        )


# ---------------------------------------------------------------------------
# Death test: Fix G — prompt render failure must be fatal, not silent fallback
# ---------------------------------------------------------------------------


def test_build_system_prompt_render_failure_propagates_from_helper():
    """DT Fix G: _build_system_prompt() must re-raise on template render failure.

    The previous inline-fallback omitted flag_definitions_block and schema context,
    causing LLM output under degraded prompt to report status='success' with
    semantically empty flags. The fallback defeated DC9 (StrictUndefined protection).

    This test verifies that _build_system_prompt itself re-raises the exception.
    The dispatch() method catches it via the outer except Exception handler and
    converts it to AnalysisOutput(status='failure') — see test below.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    dispatcher = SDKAnalysisDispatcher.__new__(SDKAnalysisDispatcher)

    def _failing_render(template_name: str, context: dict) -> str:
        raise RuntimeError("Template not found: analysis/behavior.jinja2")

    dispatcher._render = _failing_render

    with pytest.raises(RuntimeError, match="Template not found"):
        dispatcher._build_system_prompt(session_payload={"events": []})


@pytest.mark.asyncio
async def test_dispatch_returns_failure_when_prompt_render_fails():
    """DT Fix G: when template render fails, dispatch() must return AnalysisOutput(status='failure').

    The exception-free dispatch contract requires ALL exceptions to be caught.
    The previous inline fallback silently produced semantically weak output (status='success').
    The corrected behavior: fail loud → dispatch() catches and returns observable failure.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    config = _make_sdk_config()
    resolved_keys = {"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""}

    dispatcher = SDKAnalysisDispatcher(config=config, resolved_keys=resolved_keys)

    def _failing_render(template_name: str, context: dict) -> str:
        raise RuntimeError("Template not found: analysis/behavior.jinja2 — DC9 test")

    dispatcher._render = _failing_render

    output = await dispatcher.dispatch(
        session_id="sess-render-fail",
        session_payload={"events": []},
    )

    assert output.status == "failure", (
        f"Template render failure must produce status='failure', got {output.status!r}. "
        f"The exception-free dispatch contract requires ALL exceptions to be caught."
    )
    assert output.error_details is not None
    assert "RuntimeError" in str(output.error_details.get("error", "")), (
        f"error_details must include the exception type, got: {output.error_details}"
    )


# ---------------------------------------------------------------------------
# Death test: Fix E — attempt ordering assertion fires on wrong order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dc4_attempt_ordering_assertion_fires_on_wrong_order():
    """DT Fix E: if RouterChainExhaustedError.attempts has primary at wrong index,
    the assert in dispatch() must fire rather than silently swapping labels.

    This test constructs a synthetic RouterChainExhaustedError with attempts in
    wrong order (fallback first, primary second) and verifies AssertionError.
    """
    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher
    from secondsight.sdk.router import RouterChainExhaustedError, AttemptRecord

    config = _make_sdk_config()
    resolved_keys = {"anthropic": "sk-valid", "openai": "sk-openai-valid", "custom": ""}

    dispatcher = SDKAnalysisDispatcher(config=config, resolved_keys=resolved_keys)

    # Primary model name is _TEST_PRIMARY_MODEL ("claude-haiku-4-5-20251001")
    # Inject wrong order: fallback model appears at attempts[0], primary at attempts[1]
    wrong_order_attempts = [
        AttemptRecord(
            model_name=_TEST_FALLBACK_MODEL,  # fallback model first — WRONG
            exception_class="ConnectionError",
            duration_ms=100.0,
        ),
        AttemptRecord(
            model_name=_TEST_PRIMARY_MODEL,  # primary at index 1 — WRONG
            exception_class="ConnectionError",
            duration_ms=50.0,
        ),
    ]
    wrong_order_exc = RouterChainExhaustedError(
        "chain_exhausted: synthetic wrong-order test",
        attempts=wrong_order_attempts,
    )

    async def _mock_call_with_metadata(self_router: Any, **kwargs: Any) -> None:
        raise wrong_order_exc

    from unittest.mock import patch

    with patch.object(type(dispatcher._router), "call_with_metadata", _mock_call_with_metadata):
        with pytest.raises(AssertionError, match="attempts ordering violated"):
            await dispatcher.dispatch(
                session_id="sess-ordering-test",
                session_payload={"events": []},
            )


# ---------------------------------------------------------------------------
# Unit tests: Fix B — _infer_provider raises ValueError on unknown prefix
# ---------------------------------------------------------------------------


def test_infer_provider_known_prefixes():
    """Unit test: _infer_provider correctly classifies all known prefixes."""
    from secondsight.sdk.model_selection import _infer_provider

    assert _infer_provider("claude-haiku-4-5-20251001") == "anthropic"
    assert _infer_provider("claude-3-opus") == "anthropic"
    assert _infer_provider("gpt-4o-mini") == "openai"
    assert _infer_provider("gpt-4") == "openai"
    assert _infer_provider("o1-preview") == "openai"
    assert _infer_provider("o3-mini") == "openai"
    assert _infer_provider("o4-mini") == "openai"
    assert _infer_provider("gemini-2.0-flash") == "google"
    assert _infer_provider("gemini-1.5-pro") == "google"


def test_infer_provider_raises_on_unknown_prefix():
    """DT Fix B: _infer_provider must raise ValueError for unknown prefix (not default to wrong provider).

    Previous copy-A defaulted to "anthropic" for unknown prefixes.
    A gemini-* model routed through copy-A would get provider="anthropic" →
    RouterTerminalError at construction with confusing message about anthropic key.
    ValueError at _infer_provider time is more accurate.
    """
    from secondsight.sdk.model_selection import _infer_provider

    with pytest.raises(ValueError, match="unknown model name prefix"):
        _infer_provider("llama-3-8b")

    with pytest.raises(ValueError, match="unknown model name prefix"):
        _infer_provider("mistral-7b-instruct")


def test_infer_provider_gemini_is_google_not_anthropic():
    """Regression DT Fix B: gemini-* must route to 'google', not 'anthropic'.

    Previous sdk_dispatcher copy-A had no gemini case and would fall through to
    'anthropic' default, causing a confusing 401 from anthropic with a gemini model name.
    """
    from secondsight.sdk.model_selection import _infer_provider

    result = _infer_provider("gemini-2.0-flash")
    assert result == "google", (
        f"gemini-2.0-flash must resolve to 'google', got {result!r}. "
        f"Previous copy-A would return 'anthropic' — this is the regression check."
    )


# ---------------------------------------------------------------------------
# Unit test: Fix D — router.resolved_keys public property
# ---------------------------------------------------------------------------


def test_router_resolved_keys_public_property_returns_snapshot_copy():
    """Unit test Fix D: LLMRouter.resolved_keys property returns a copy, not the internal dict."""
    from secondsight.sdk.router import LLMRouter
    from secondsight.sdk._specs import ModelSpec

    original_keys = {"anthropic": "sk-test-key", "openai": "", "custom": ""}
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=original_keys,
    )

    keys = router.resolved_keys
    assert keys["anthropic"] == "sk-test-key"

    # Mutating the returned dict must NOT affect the internal snapshot
    keys["anthropic"] = "sk-mutated-external"
    assert router.resolved_keys["anthropic"] == "sk-test-key", (
        "Mutating the returned dict must NOT affect the router's internal snapshot. "
        "resolved_keys property must return a copy, not a reference."
    )


def test_router_resolved_keys_no_private_access_needed():
    """Unit test Fix D: router.resolved_keys (public) returns same content as router._resolved_keys.

    Verifies the property is a faithful representation so agent.py can switch
    from router._resolved_keys to router.resolved_keys without behavioral change.
    """
    from secondsight.sdk.router import LLMRouter
    from secondsight.sdk._specs import ModelSpec

    resolved_keys = {"anthropic": "sk-pub-test", "openai": "sk-openai-test", "custom": ""}
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )

    public = router.resolved_keys
    private = router._resolved_keys  # type: ignore[attr-defined]

    assert public == private, (
        f"router.resolved_keys (public) must return same content as router._resolved_keys. "
        f"Got public={public!r}, private={private!r}"
    )


# ---------------------------------------------------------------------------
# Unit test: Fix A — AnalysisDispatcher Protocol exists and is importable
# ---------------------------------------------------------------------------


def test_analysis_dispatcher_protocol_is_importable():
    """Unit test Fix A: AnalysisDispatcher Protocol must be importable from analysis.dispatcher."""
    from secondsight.analysis.dispatcher import AnalysisDispatcher  # noqa: F401

    assert hasattr(AnalysisDispatcher, "dispatch"), (
        "AnalysisDispatcher Protocol must define a dispatch() method"
    )
