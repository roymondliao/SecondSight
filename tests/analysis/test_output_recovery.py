"""Tests for shared output recovery helpers (Phase 1 Task 2).

Death tests first:
- fenced JSON normalization
- preface/suffix JSON object extraction
- malformed JSON is not silently "fixed"
- feedback output is bounded
- validation-error classification distinguishes json_decode from schema_mismatch
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import litellm
import pytest
from pydantic import ValidationError


class TestDeathClassification:
    def test_executor_evidence_classification_preserves_source_confidence_and_executor(
        self,
    ) -> None:
        from secondsight.analysis.output_recovery import (
            EvidenceConfidence,
            ExecutorFailureEvidence,
            FailureClass,
            classify_output_failure,
        )

        failure = classify_output_failure(
            RuntimeError("raw CLI failed"),
            evidence=ExecutorFailureEvidence(
                source="cli_stdout_envelope",
                executor="claude_code",
                failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
                reason="fatal_auth_or_config",
                message="Claude reported invalid credentials",
                confidence=EvidenceConfidence.DERIVED,
                raw={"subtype": "error_during_execution", "api_error_status": 401},
            ),
        )

        assert failure.failure_class is FailureClass.FATAL_AUTH_OR_CONFIG
        assert failure.reason == "fatal_auth_or_config"
        assert failure.error == "Claude reported invalid credentials"
        assert failure.details["evidence_source"] == "cli_stdout_envelope"
        assert failure.details["evidence_confidence"] == "derived"
        assert failure.details["evidence_executor"] == "claude_code"
        assert failure.details["api_error_status"] == 401

    def test_unusable_executor_evidence_fails_loud_with_low_confidence_metadata(self) -> None:
        from secondsight.analysis.output_recovery import (
            EvidenceConfidence,
            ExecutorFailureEvidence,
            FailureClass,
            classify_output_failure,
        )

        failure = classify_output_failure(
            RuntimeError("ambiguous CLI failure"),
            evidence=ExecutorFailureEvidence(
                source="cli_exit",
                executor="codex",
                message="ambiguous failure text",
                confidence=EvidenceConfidence.UNKNOWN,
                raw={"stdout_excerpt": "something failed"},
            ),
        )

        assert failure.failure_class is FailureClass.FATAL_EXECUTION_ERROR
        assert failure.reason == "fatal_execution_error"
        assert failure.details["evidence_confidence"] == "unknown"
        assert failure.details["evidence_source"] == "cli_exit"
        assert failure.details["evidence_executor"] == "codex"

    def test_executor_evidence_raw_collisions_do_not_overwrite_recovery_envelope(
        self,
    ) -> None:
        from secondsight.analysis.output_recovery import (
            EvidenceConfidence,
            ExecutorFailureEvidence,
            FailureClass,
            RetryMode,
            build_recovery_error_details,
            classify_output_failure,
        )

        failure = classify_output_failure(
            RuntimeError("raw CLI failed"),
            evidence=ExecutorFailureEvidence(
                source="cli_exit",
                executor="claude_code",
                failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
                reason="fatal_auth_or_config",
                confidence=EvidenceConfidence.HEURISTIC,
                raw={
                    "reason": "raw reason",
                    "failure_class": "raw failure class",
                    "retry_mode": "raw retry mode",
                    "attempts": 99,
                },
            ),
        )

        details = build_recovery_error_details(
            reason=failure.reason,
            failure_class=failure.failure_class,
            attempts=1,
            retry_exhausted=False,
            retry_mode=RetryMode.NONE,
            error=failure.error,
            extra_error_details=failure.details,
        )

        assert details["reason"] == "fatal_auth_or_config"
        assert details["failure_class"] == "fatal_auth_or_config"
        assert details["retry_mode"] == "none"
        assert details["attempts"] == 1
        assert details["evidence_source"] == "cli_exit"
        assert details["evidence_confidence"] == "heuristic"
        assert details["raw_error_details"] == {
            "reason": "raw reason",
            "failure_class": "raw failure class",
            "retry_mode": "raw retry mode",
            "attempts": 99,
        }

    def test_transport_timeout_is_classified_separately_from_json_and_schema_failures(self) -> None:
        from secondsight.analysis.output import AnalysisOutput
        from secondsight.analysis.output_recovery import FailureClass, classify_output_failure

        with pytest.raises(ValidationError) as json_exc:
            AnalysisOutput.model_validate_json("not json at all")

        schema_payload = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError) as schema_exc:
            AnalysisOutput.model_validate(schema_payload)

        timeout_failure = classify_output_failure(asyncio.TimeoutError("provider call timed out"))
        json_failure = classify_output_failure(json_exc.value)
        schema_failure = classify_output_failure(schema_exc.value)

        assert timeout_failure.failure_class is FailureClass.TRANSPORT_TIMEOUT
        assert timeout_failure.reason == "transport_timeout"
        assert json_failure.failure_class is FailureClass.JSON_DECODE
        assert schema_failure.failure_class is FailureClass.SCHEMA_MISMATCH

    def test_fatal_auth_or_config_is_no_retry(self) -> None:
        from secondsight.analysis.output_recovery import (
            RetryMode,
            classify_output_failure,
            decide_retry,
        )
        from secondsight.sdk.router import RouterTerminalError

        failure = classify_output_failure(
            RouterTerminalError(
                "no provider keys resolvable: primary provider 'openai' has empty key in "
                "resolved_keys"
            )
        )
        decision = decide_retry(failure, attempt_number=1, max_attempts=3, feedback_max_chars=200)

        assert failure.reason == "fatal_auth_or_config"
        assert decision.should_retry is False
        assert decision.retry_mode is RetryMode.NONE
        assert decision.retry_feedback == ""

    def test_retry_decision_objects_are_serializable_for_logs(self) -> None:
        from secondsight.analysis.output_recovery import (
            ClassifiedFailure,
            FailureClass,
            decide_retry,
        )

        decision = decide_retry(
            ClassifiedFailure(
                failure_class=FailureClass.SCHEMA_MISMATCH,
                reason="schema_mismatch",
                error="missing field",
                details={"field_errors": ["session_summary: Field required"]},
            ),
            attempt_number=1,
            max_attempts=3,
            feedback_max_chars=200,
        )

        payload = decision.to_log_dict()

        assert json.loads(json.dumps(payload))["failure_class"] == "schema_mismatch"

    def test_router_auth_exhaustion_is_fatal_auth_no_retry(self) -> None:
        from secondsight.analysis.output_recovery import (
            FailureClass,
            RetryMode,
            classify_output_failure,
            decide_retry,
        )
        from secondsight.sdk.router import AttemptRecord, RouterChainExhaustedError

        failure = classify_output_failure(
            RouterChainExhaustedError(
                "chain_exhausted after repeated auth failures",
                attempts=[
                    AttemptRecord(
                        model_name="gpt-4o-mini",
                        exception_class="AuthenticationError",
                        duration_ms=125.0,
                    )
                ],
            )
        )
        decision = decide_retry(failure, attempt_number=1, max_attempts=3, feedback_max_chars=200)

        assert failure.failure_class is FailureClass.FATAL_AUTH_OR_CONFIG
        assert decision.should_retry is False
        assert decision.retry_mode is RetryMode.NONE

    def test_foreign_attempts_container_is_not_silently_treated_as_router_transport(self) -> None:
        from secondsight.analysis.output_recovery import FailureClass, classify_output_failure

        class ForeignAttemptsError(Exception):
            def __init__(self) -> None:
                super().__init__("foreign attempts object")
                self.attempts = [{"exception_class": "TimeoutError"}]

        failure = classify_output_failure(ForeignAttemptsError())

        assert failure.failure_class is FailureClass.FATAL_EXECUTION_ERROR


class TestNormalization:
    def test_fenced_json_normalizes_without_changing_inner_json(self) -> None:
        from secondsight.analysis.output_recovery import normalize_llm_json_text

        inner = '{"schema_version":"1.0"}'
        result = normalize_llm_json_text(f"```json\n{inner}\n```")

        assert result.normalized_text == inner
        assert result.changed is True

    def test_preface_suffix_noise_trims_to_first_top_level_object(self) -> None:
        from secondsight.analysis.output_recovery import normalize_llm_json_text

        raw = 'Here is the result.\n{"a": 1, "b": {"c": 2}}\nThanks.'
        result = normalize_llm_json_text(raw)

        assert result.normalized_text == '{"a": 1, "b": {"c": 2}}'
        assert result.changed is True

    def test_malformed_json_is_not_silently_fixed(self) -> None:
        from secondsight.analysis.output_recovery import normalize_llm_json_text

        raw = '{"a": 1'
        result = normalize_llm_json_text(raw)

        assert result.normalized_text == raw
        assert result.changed is False


class TestFeedback:
    def test_feedback_builder_output_is_bounded(self) -> None:
        from secondsight.analysis.output_recovery import (
            ClassifiedFailure,
            FailureClass,
            build_retry_feedback,
        )

        failure = ClassifiedFailure(
            failure_class=FailureClass.SCHEMA_MISMATCH,
            reason="schema_mismatch",
            error="x" * 500,
            details={"field_errors": ["missing session_summary", "bad retry_count"]},
        )

        feedback = build_retry_feedback(failure, max_chars=80)
        assert len(feedback) <= 80

    def test_feedback_builder_honors_tiny_bounds(self) -> None:
        from secondsight.analysis.output_recovery import (
            ClassifiedFailure,
            FailureClass,
            build_retry_feedback,
        )

        failure = ClassifiedFailure(
            failure_class=FailureClass.JSON_DECODE,
            reason="json_decode",
            error="not json",
        )

        assert build_retry_feedback(failure, max_chars=1) == "."
        assert build_retry_feedback(failure, max_chars=2) == ".."

    def test_transport_failures_do_not_emit_schema_fix_instructions(self) -> None:
        from secondsight.analysis.output_recovery import (
            ClassifiedFailure,
            FailureClass,
            build_retry_feedback,
        )

        failure = ClassifiedFailure(
            failure_class=FailureClass.TRANSPORT_TIMEOUT,
            reason="transport_timeout",
            error="provider timed out",
        )

        feedback = build_retry_feedback(failure, max_chars=200)

        assert "schema" not in feedback.lower()
        assert "json" not in feedback.lower()


class TestClassification:
    def test_classifier_distinguishes_json_decode_from_schema_mismatch(self) -> None:
        from secondsight.analysis.output import AnalysisOutput
        from secondsight.analysis.output_recovery import (
            FailureClass,
            classify_output_failure,
        )

        with pytest.raises(ValidationError) as json_exc:
            AnalysisOutput.model_validate_json("not json at all")

        schema_payload = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError) as schema_exc:
            AnalysisOutput.model_validate(schema_payload)

        json_failure = classify_output_failure(json_exc.value)
        schema_failure = classify_output_failure(schema_exc.value)

        assert json_failure.failure_class is FailureClass.JSON_DECODE
        assert schema_failure.failure_class is FailureClass.SCHEMA_MISMATCH

    def test_classifier_handles_standalone_json_decode_error(self) -> None:
        from secondsight.analysis.output_recovery import FailureClass, classify_output_failure

        try:
            json.loads("not json")
        except json.JSONDecodeError as exc:
            failure = classify_output_failure(exc)
        else:
            raise AssertionError("json.loads('not json') must raise JSONDecodeError")

        assert failure.failure_class is FailureClass.JSON_DECODE

    def test_empty_output_classified_as_retryable_json_decode(self) -> None:
        from secondsight.analysis.output_recovery import FailureClass, classify_empty_output

        failure = classify_empty_output(source="stdout")

        assert failure.failure_class is FailureClass.JSON_DECODE
        assert failure.reason == "json_decode"
        assert failure.details == {"condition": "empty_output", "source": "stdout"}

    @pytest.mark.parametrize(
        ("exception_class", "expected_failure_class"),
        [
            ("TimeoutError", "transport_timeout"),
            ("RateLimitError", "transport_rate_limit"),
            ("APIConnectionError", "transport_api_error"),
        ],
    )
    def test_router_chain_attempt_trace_maps_to_shared_transport_taxonomy(
        self,
        exception_class: str,
        expected_failure_class: str,
    ) -> None:
        from secondsight.analysis.output_recovery import classify_output_failure
        from secondsight.sdk.router import AttemptRecord, RouterChainExhaustedError

        failure = classify_output_failure(
            RouterChainExhaustedError(
                "chain_exhausted",
                attempts=[
                    AttemptRecord(
                        model_name="gpt-4o-mini",
                        exception_class=exception_class,
                        duration_ms=125.0,
                    )
                ],
            )
        )

        assert failure.reason == expected_failure_class
        assert failure.details["attempt_classes"] == [exception_class]

    def test_classifier_walks_exception_cause_for_auth_config_failures(self) -> None:
        from secondsight.analysis.output_recovery import FailureClass, classify_output_failure

        try:
            try:
                raise RuntimeError("inner auth failure")
            except RuntimeError as exc:
                raise litellm.AuthenticationError(
                    message="bad key",
                    llm_provider="openai",
                    model="gpt-4o-mini",
                ) from exc
        except litellm.AuthenticationError as exc:
            wrapped = RuntimeError("wrapped config error")
            wrapped.__cause__ = exc
            failure = classify_output_failure(wrapped)
        else:
            raise AssertionError("authentication error must be raised")

        assert failure.failure_class is FailureClass.FATAL_AUTH_OR_CONFIG


class TestRetryDecisions:
    def test_schema_mismatch_retries_with_output_repair_feedback(self) -> None:
        from secondsight.analysis.output_recovery import (
            RetryMode,
            classify_output_failure,
            decide_retry,
        )
        from secondsight.analysis.output import AnalysisOutput

        schema_payload = {
            "schema_version": "1.0",
            "session_id": "sess-001",
            "status": "success",
            "behavior_flags": [],
            "dispatched_via": "sdk",
            "primary_model": "claude-opus-4-5",
        }
        with pytest.raises(ValidationError) as schema_exc:
            AnalysisOutput.model_validate(schema_payload)

        failure = classify_output_failure(schema_exc.value)
        decision = decide_retry(failure, attempt_number=1, max_attempts=3, feedback_max_chars=200)

        assert decision.should_retry is True
        assert decision.retry_mode is RetryMode.OUTPUT_REPAIR
        assert decision.retry_feedback

    def test_transport_failures_are_classified_but_do_not_use_output_repair_budget(self) -> None:
        from secondsight.analysis.output_recovery import (
            ClassifiedFailure,
            FailureClass,
            RetryMode,
            decide_retry,
        )

        failure = ClassifiedFailure(
            failure_class=FailureClass.TRANSPORT_TIMEOUT,
            reason="transport_timeout",
            error="provider timed out",
        )

        decision = decide_retry(failure, attempt_number=1, max_attempts=3, feedback_max_chars=200)

        assert decision.should_retry is False
        assert decision.retry_mode is RetryMode.TRANSPORT
        assert decision.reason == "transport_timeout"
        assert decision.next_attempt_number is None
        assert decision.retry_feedback == ""

    def test_merge_recovery_error_details_preserves_base_fields_on_collision(self) -> None:
        from secondsight.analysis.output_recovery import merge_recovery_error_details

        merged = merge_recovery_error_details(
            {
                "reason": "schema_mismatch",
                "failure_class": "schema_mismatch",
                "error": "validated error",
            },
            {
                "reason": "raw reason",
                "error": "raw provider error",
                "request_id": "req-123",
            },
        )

        assert merged["reason"] == "schema_mismatch"
        assert merged["failure_class"] == "schema_mismatch"
        assert merged["error"] == "validated error"
        assert merged["request_id"] == "req-123"
        assert merged["raw_error_details"] == {
            "reason": "raw reason",
            "error": "raw provider error",
        }

    def test_build_recovery_error_details_uses_shared_envelope_and_raw_collisions(self) -> None:
        from secondsight.analysis.output_recovery import (
            FailureClass,
            RetryMode,
            build_recovery_error_details,
        )

        details = build_recovery_error_details(
            reason="transport_timeout",
            failure_class=FailureClass.TRANSPORT_TIMEOUT,
            attempts=1,
            retry_exhausted=False,
            retry_mode=RetryMode.TRANSPORT,
            error="canonical error",
            extra_error_details={"error": "raw provider error", "request_id": "req-123"},
            additional_error_details={"reason": "raw reason"},
        )

        assert details["reason"] == "transport_timeout"
        assert details["failure_class"] == "transport_timeout"
        assert details["attempts"] == 1
        assert details["retry_exhausted"] is False
        assert details["retry_mode"] == "transport"
        assert details["error"] == "canonical error"
        assert details["request_id"] == "req-123"
        assert details["raw_error_details"] == {
            "error": "raw provider error",
            "reason": "raw reason",
        }

    def test_recovery_error_details_redacts_secret_values_and_bounds_strings(self) -> None:
        from secondsight.analysis.output_recovery import (
            FailureClass,
            RetryMode,
            build_recovery_error_details,
        )

        details = build_recovery_error_details(
            reason="fatal_auth_or_config",
            failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
            attempts=1,
            retry_exhausted=False,
            retry_mode=RetryMode.NONE,
            error=(
                "provider rejected api_key=sk-ant-test-secret-1234567890 "
                "Authorization: Bearer very-secret-token-value"
            ),
            extra_error_details={
                "stderr": "token=abc1234567890 " + ("x" * 3_000),
                "nested": {"credential": "credential=super-secret-value"},
            },
        )

        rendered = str(details)
        assert "sk-ant-test-secret-1234567890" not in rendered
        assert "very-secret-token-value" not in rendered
        assert "abc1234567890" not in rendered
        assert "super-secret-value" not in rendered
        assert "[REDACTED]" in details["error"]
        assert len(details["stderr"]) <= 2_000

    def test_recovery_trace_is_serializable_and_preserves_forensics(self) -> None:
        from secondsight.analysis.output_recovery import (
            FailureClass,
            RecoveryAttempt,
            RecoveryTrace,
            RetryDecision,
            RetryMode,
        )

        trace = RecoveryTrace(
            attempts=[
                RecoveryAttempt(
                    attempt_number=1,
                    executor="sdk",
                    failure_class=FailureClass.TRANSPORT_TIMEOUT,
                    reason="transport_timeout",
                    error="provider timed out",
                    details={"provider": "openai", "request_id": "req-123"},
                )
            ],
            final_decision=RetryDecision(
                should_retry=False,
                retry_mode=RetryMode.TRANSPORT,
                reason="transport_timeout",
                failure_class=FailureClass.TRANSPORT_TIMEOUT,
                attempt_number=1,
                max_attempts=3,
                next_attempt_number=None,
            ),
        )

        payload = trace.to_log_dict()

        assert payload["attempts"][0]["details"]["request_id"] == "req-123"

    def test_recovery_trace_rejects_non_retry_decision_final_decision(self) -> None:
        from secondsight.analysis.output_recovery import RecoveryTrace

        bad_final_decision: Any = {"should_retry": True}
        with pytest.raises(TypeError):
            RecoveryTrace(final_decision=bad_final_decision)
