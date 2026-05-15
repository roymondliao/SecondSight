"""Tests for shared output recovery helpers (Phase 1 Task 2).

Death tests first:
- fenced JSON normalization
- preface/suffix JSON object extraction
- malformed JSON is not silently "fixed"
- feedback output is bounded
- validation-error classification distinguishes json_decode from schema_mismatch
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError


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
