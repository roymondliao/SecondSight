"""Shared helpers for analysis output recovery.

Phase 1 uses these helpers in the CLI dispatcher only. Phase 2 will extend the
same contract to SDK dispatch as well.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from pydantic import ValidationError


class FailureClass(str, Enum):
    """Normalized output-failure taxonomy for recovery decisions."""

    NORMALIZABLE_FORMAT_ERROR = "normalizable_format_error"
    JSON_DECODE = "json_decode"
    SCHEMA_MISMATCH = "schema_mismatch"
    FATAL_EXECUTION_ERROR = "fatal_execution_error"


@dataclass(frozen=True)
class NormalizationResult:
    """Result of normalizing a raw model response prior to JSON validation."""

    normalized_text: str
    changed: bool
    strategy: str | None = None


@dataclass(frozen=True)
class ClassifiedFailure:
    """Structured failure details suitable for retry policy + feedback."""

    failure_class: FailureClass
    reason: str
    error: str
    details: dict[str, Any] = field(default_factory=dict)


_FENCED_JSON_PATTERN = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def normalize_llm_json_text(raw: str) -> NormalizationResult:
    """Normalize common wrapper noise around an otherwise valid JSON object.

    Supported repairs:
    - strip outer markdown fences
    - trim leading/trailing chatter by extracting the first balanced top-level
      JSON object

    This function intentionally does NOT try to repair malformed JSON syntax.
    """

    stripped = raw.strip()
    if not stripped:
        return NormalizationResult(normalized_text=stripped, changed=False)

    fenced_match = _FENCED_JSON_PATTERN.match(stripped)
    if fenced_match:
        inner = fenced_match.group(1).strip()
        return NormalizationResult(normalized_text=inner, changed=True, strategy="strip_fence")

    extracted = _extract_first_json_object(stripped)
    if extracted is not None and extracted != stripped:
        return NormalizationResult(
            normalized_text=extracted,
            changed=True,
            strategy="extract_first_object",
        )

    return NormalizationResult(normalized_text=stripped, changed=False)


def classify_output_failure(exc: Exception) -> ClassifiedFailure:
    """Classify output/validation failures into shared recovery categories."""

    if isinstance(exc, json.JSONDecodeError):
        return ClassifiedFailure(
            failure_class=FailureClass.JSON_DECODE,
            reason="json_decode",
            error=str(exc),
        )

    if isinstance(exc, ValidationError):
        error_types = {e["type"] for e in exc.errors()}
        if error_types and error_types <= {"json_invalid"}:
            return ClassifiedFailure(
                failure_class=FailureClass.JSON_DECODE,
                reason="json_decode",
                error=str(exc),
                details={"error_types": sorted(error_types)},
            )

        field_errors = [_format_validation_error(err) for err in exc.errors()]
        return ClassifiedFailure(
            failure_class=FailureClass.SCHEMA_MISMATCH,
            reason="schema_mismatch",
            error=str(exc),
            details={"field_errors": field_errors},
        )

    return ClassifiedFailure(
        failure_class=FailureClass.FATAL_EXECUTION_ERROR,
        reason="fatal_execution_error",
        error=str(exc),
    )


def classify_empty_output(*, source: str = "stdout") -> ClassifiedFailure:
    """Represent an empty model output as a retryable JSON-decode failure."""

    return ClassifiedFailure(
        failure_class=FailureClass.JSON_DECODE,
        reason="json_decode",
        error=f"Empty {source} -- no JSON to parse",
        details={"condition": "empty_output", "source": source},
    )


def build_retry_feedback(failure: ClassifiedFailure, *, max_chars: int) -> str:
    """Build bounded, structured retry feedback for the model."""

    if max_chars <= 0:
        return ""

    if failure.failure_class is FailureClass.JSON_DECODE:
        parts = [
            "Previous output was not valid JSON.",
            "Return exactly one JSON object.",
            "Do not include markdown fences.",
            "Do not include text before or after the JSON.",
        ]
        if failure.error:
            parts.append(f"Parser error: {failure.error}")
        message = "\n".join(parts)
    elif failure.failure_class is FailureClass.SCHEMA_MISMATCH:
        parts = [
            "Previous output did not match the required JSON schema.",
            "Fix the schema issues below and return exactly one JSON object.",
        ]
        for item in failure.details.get("field_errors", []):
            parts.append(f"- {item}")
        message = "\n".join(parts)
    else:
        message = (
            f"Previous output failed with a non-recoverable execution error. Error: {failure.error}"
        )

    if len(message) <= max_chars:
        return message
    if max_chars <= 3:
        return "." * max_chars
    return message[: max_chars - 3] + "..."


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced top-level JSON object from text, if any."""

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for idx in range(start, len(text)):
        ch = text[idx]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    return None


def _format_validation_error(error: Mapping[str, Any]) -> str:
    """Render a Pydantic validation error into concise retry feedback text."""

    loc = ".".join(str(part) for part in error.get("loc", ()))
    msg = str(error.get("msg", "validation error"))
    if loc:
        return f"{loc}: {msg}"
    return msg


__all__ = [
    "ClassifiedFailure",
    "FailureClass",
    "NormalizationResult",
    "build_retry_feedback",
    "classify_empty_output",
    "classify_output_failure",
    "normalize_llm_json_text",
]
