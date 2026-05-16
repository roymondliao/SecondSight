"""Shared helpers for analysis output recovery.

Phase 1 uses these helpers in the CLI dispatcher only. Phase 2 will extend the
same contract to SDK dispatch as well.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Mapping

import httpx
import litellm
import pydantic_ai.exceptions

from pydantic import ValidationError


class FailureClass(str, Enum):
    """Normalized output-failure taxonomy for recovery decisions."""

    NORMALIZABLE_FORMAT_ERROR = "normalizable_format_error"
    JSON_DECODE = "json_decode"
    SCHEMA_MISMATCH = "schema_mismatch"
    TRANSPORT_TIMEOUT = "transport_timeout"
    TRANSPORT_RATE_LIMIT = "transport_rate_limit"
    TRANSPORT_API_ERROR = "transport_api_error"
    FATAL_AUTH_OR_CONFIG = "fatal_auth_or_config"
    FATAL_EXECUTION_ERROR = "fatal_execution_error"


class RetryMode(str, Enum):
    """Shared retry mode without taking over executor ownership."""

    NONE = "none"
    OUTPUT_REPAIR = "output_repair"
    TRANSPORT = "transport"


class EvidenceConfidence(str, Enum):
    """How strongly executor evidence supports a classification."""

    TYPED = "typed"
    DERIVED = "derived"
    HEURISTIC = "heuristic"
    UNKNOWN = "unknown"


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

    def to_log_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for logs / error_details."""

        return _json_safe(asdict(self))


@dataclass(frozen=True)
class ExecutorFailureEvidence:
    """Executor-owned failure evidence before shared recovery classification.

    The shared layer may trust stable fields here, but raw executor/provider
    wording belongs to the adapter that creates this evidence.
    """

    source: str
    executor: str
    failure_class: FailureClass | str | None = None
    reason: str | None = None
    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)
    confidence: EvidenceConfidence | str = EvidenceConfidence.UNKNOWN

    def to_failure_details(self) -> dict[str, Any]:
        """Return evidence metadata plus raw adapter details for error_details."""

        confidence = (
            self.confidence.value
            if isinstance(self.confidence, EvidenceConfidence)
            else str(self.confidence)
        )
        details: dict[str, Any] = {
            "evidence_source": self.source,
            "evidence_confidence": confidence,
            "evidence_executor": self.executor,
        }
        if self.reason:
            details["evidence_reason"] = self.reason

        for key, value in self.raw.items():
            if key in details:
                raw_error_details = details.setdefault("raw_error_details", {})
                if isinstance(raw_error_details, dict):
                    raw_error_details[key] = value
                continue
            details[str(key)] = value

        return sanitize_error_details(details)


@dataclass(frozen=True)
class RetryDecision:
    """Retry policy result shared by CLI and SDK dispatchers."""

    should_retry: bool
    retry_mode: RetryMode
    reason: str
    failure_class: FailureClass
    attempt_number: int
    max_attempts: int
    next_attempt_number: int | None
    retry_feedback: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for logs / error_details."""

        return _json_safe(asdict(self))


@dataclass(frozen=True)
class RecoveryAttempt:
    """One shared recovery attempt with executor-specific forensics preserved."""

    attempt_number: int
    executor: str
    failure_class: FailureClass
    reason: str
    error: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for logs / error_details."""

        return _json_safe(asdict(self))


@dataclass(frozen=True)
class RecoveryTrace:
    """Serializable trace of recovery attempts plus the last decision."""

    attempts: list[RecoveryAttempt] = field(default_factory=list)
    final_decision: RetryDecision | None = None

    def __post_init__(self) -> None:
        if self.final_decision is not None and not isinstance(self.final_decision, RetryDecision):
            raise TypeError("final_decision must be a RetryDecision or None")

    def to_log_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for logs / error_details."""

        payload: dict[str, Any] = {"attempts": [attempt.to_log_dict() for attempt in self.attempts]}
        if self.final_decision is not None:
            payload["final_decision"] = self.final_decision.to_log_dict()
        return payload


_FENCED_JSON_PATTERN = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_MAX_ERROR_DETAIL_STRING_LENGTH = 2_000
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})\b"),
    re.compile(
        r"\b((?:api[_-]?key|token|credential|password|secret)\s*[:=]\s*)([^\s,;]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})\b", re.IGNORECASE),
)


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


def classify_output_failure(
    exc: Exception,
    *,
    evidence: ExecutorFailureEvidence | None = None,
) -> ClassifiedFailure:
    """Classify output/validation failures into shared recovery categories."""

    if evidence is not None:
        return _classify_executor_evidence(exc, evidence)

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ClassifiedFailure(
            failure_class=FailureClass.TRANSPORT_TIMEOUT,
            reason="transport_timeout",
            error=str(exc),
        )

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

    attempt_trace_classification = _classify_transport_attempt_trace(exc)
    if attempt_trace_classification is not None:
        return attempt_trace_classification

    if _is_transport_timeout_error(exc):
        return ClassifiedFailure(
            failure_class=FailureClass.TRANSPORT_TIMEOUT,
            reason="transport_timeout",
            error=str(exc),
        )

    if _is_transport_rate_limit_error(exc):
        return ClassifiedFailure(
            failure_class=FailureClass.TRANSPORT_RATE_LIMIT,
            reason="transport_rate_limit",
            error=str(exc),
        )

    if _is_fatal_auth_or_config_error(exc):
        return ClassifiedFailure(
            failure_class=FailureClass.FATAL_AUTH_OR_CONFIG,
            reason="fatal_auth_or_config",
            error=str(exc),
        )

    if _is_transport_api_error(exc):
        return ClassifiedFailure(
            failure_class=FailureClass.TRANSPORT_API_ERROR,
            reason="transport_api_error",
            error=str(exc),
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
    elif failure.failure_class is FailureClass.NORMALIZABLE_FORMAT_ERROR:
        parts = [
            "Previous output was wrapped in extra formatting.",
            "Return exactly one JSON object with no surrounding commentary.",
        ]
        if failure.error:
            parts.append(f"Formatting issue: {failure.error}")
        message = "\n".join(parts)
    elif failure.failure_class in {
        FailureClass.TRANSPORT_TIMEOUT,
        FailureClass.TRANSPORT_RATE_LIMIT,
        FailureClass.TRANSPORT_API_ERROR,
    }:
        message = (
            "Previous attempt failed before a usable response arrived. "
            "Retry handling stays at the executor layer."
        )
    else:
        message = (
            f"Previous output failed with a non-recoverable execution error. Error: {failure.error}"
        )

    if len(message) <= max_chars:
        return message
    if max_chars <= 3:
        return "." * max_chars
    return message[: max_chars - 3] + "..."


def decide_retry(
    failure: ClassifiedFailure,
    *,
    attempt_number: int,
    max_attempts: int,
    feedback_max_chars: int,
) -> RetryDecision:
    """Return shared retry semantics for a classified failure."""

    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    attempts_remaining = max(max_attempts - attempt_number, 0)
    retry_mode = _retry_mode_for_failure_class(failure.failure_class)
    should_retry = retry_mode is RetryMode.OUTPUT_REPAIR and attempt_number < max_attempts
    retry_feedback = ""

    if should_retry and retry_mode is RetryMode.OUTPUT_REPAIR:
        retry_feedback = build_retry_feedback(failure, max_chars=feedback_max_chars)

    if should_retry:
        reason = failure.reason
    elif retry_mode is RetryMode.OUTPUT_REPAIR and attempt_number >= max_attempts:
        reason = "retry_exhausted"
    else:
        reason = failure.reason
    next_attempt_number = attempt_number + 1 if should_retry else None

    return RetryDecision(
        should_retry=should_retry,
        retry_mode=retry_mode,
        reason=reason,
        failure_class=failure.failure_class,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        next_attempt_number=next_attempt_number,
        retry_feedback=retry_feedback,
        details={"attempts_remaining": attempts_remaining},
    )


def merge_recovery_error_details(
    base_error_details: Mapping[str, Any],
    *extra_error_details: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge raw executor/provider evidence without overwriting shared fields.

    Any incoming key that already exists in the base payload is preserved under
    ``raw_error_details``. This keeps the observability contract stable while
    retaining raw forensics for debugging.
    """

    merged = dict(base_error_details)
    protected_keys = set(merged) | {"raw_error_details"}
    raw_collisions: dict[str, Any] = {}

    for details in extra_error_details:
        if not details:
            continue
        for key, value in details.items():
            if key in protected_keys:
                raw_collisions[key] = value
                continue
            merged[key] = value
            protected_keys.add(key)

    if raw_collisions:
        existing_raw = merged.get("raw_error_details")
        if isinstance(existing_raw, Mapping):
            raw_error_details = dict(existing_raw)
        elif existing_raw is None:
            raw_error_details = {}
        else:
            raw_error_details = {"existing_raw_error_details": existing_raw}
        raw_error_details.update(raw_collisions)
        merged["raw_error_details"] = raw_error_details

    return sanitize_error_details(merged)


def sanitize_error_details(value: Any) -> Any:
    """Return JSON-safe error details with secrets redacted and strings bounded."""

    if isinstance(value, Mapping):
        return {str(key): sanitize_error_details(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_error_details(item) for item in value]
    if isinstance(value, str):
        return _sanitize_error_detail_string(value)
    return _json_safe(value)


def build_recovery_error_details(
    *,
    reason: str,
    failure_class: FailureClass | str,
    attempts: int,
    retry_exhausted: bool,
    retry_mode: RetryMode | str,
    error: str = "",
    exit_code: int | None = None,
    stderr: str = "",
    message: str = "",
    retry_feedback: str = "",
    recovery_trace: Mapping[str, Any] | None = None,
    extra_error_details: Mapping[str, Any] | None = None,
    additional_error_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical recovery error_details envelope.

    CLI and SDK dispatchers may add executor-specific fields, but the shared
    observability keys are assembled in one place to prevent drift.
    """

    error_details: dict[str, Any] = {
        "reason": reason,
        "failure_class": failure_class.value
        if isinstance(failure_class, FailureClass)
        else failure_class,
        "attempts": attempts,
        "retry_exhausted": retry_exhausted,
        "retry_mode": retry_mode.value if isinstance(retry_mode, RetryMode) else retry_mode,
    }
    if error:
        error_details["error"] = error
    if exit_code is not None:
        error_details["exit_code"] = exit_code
    if stderr:
        error_details["stderr"] = stderr
    if message:
        error_details["message"] = message
    if retry_feedback:
        error_details["retry_feedback"] = retry_feedback
    if recovery_trace is not None:
        error_details["recovery_trace"] = _json_safe(recovery_trace)

    return merge_recovery_error_details(
        error_details,
        extra_error_details,
        additional_error_details,
    )


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


def _retry_mode_for_failure_class(failure_class: FailureClass) -> RetryMode:
    if failure_class in {
        FailureClass.NORMALIZABLE_FORMAT_ERROR,
        FailureClass.JSON_DECODE,
        FailureClass.SCHEMA_MISMATCH,
    }:
        return RetryMode.OUTPUT_REPAIR
    if failure_class in {
        FailureClass.TRANSPORT_TIMEOUT,
        FailureClass.TRANSPORT_RATE_LIMIT,
        FailureClass.TRANSPORT_API_ERROR,
    }:
        return RetryMode.TRANSPORT
    return RetryMode.NONE


def _classify_executor_evidence(
    exc: Exception,
    evidence: ExecutorFailureEvidence,
) -> ClassifiedFailure:
    details = evidence.to_failure_details()
    failure_class = _coerce_failure_class(evidence.failure_class)

    if failure_class is None:
        return ClassifiedFailure(
            failure_class=FailureClass.FATAL_EXECUTION_ERROR,
            reason="fatal_execution_error",
            error=evidence.message or str(exc),
            details=details,
        )

    return ClassifiedFailure(
        failure_class=failure_class,
        reason=evidence.reason or failure_class.value,
        error=evidence.message or str(exc),
        details=details,
    )


def _coerce_failure_class(value: FailureClass | str | None) -> FailureClass | None:
    if value is None:
        return None
    if isinstance(value, FailureClass):
        return value
    try:
        return FailureClass(str(value))
    except ValueError:
        return None


def _classify_transport_attempt_trace(exc: Exception) -> ClassifiedFailure | None:
    from secondsight.sdk.router import AttemptRecord, RouterChainExhaustedError

    if not isinstance(exc, RouterChainExhaustedError):
        return None

    attempts = exc.attempts
    if not attempts or not all(isinstance(attempt, AttemptRecord) for attempt in attempts):
        return None

    ordered_classes = [str(attempt.exception_class) for attempt in attempts]
    failure_class = classify_attempt_failure_class(ordered_classes, terminal=False)

    return ClassifiedFailure(
        failure_class=failure_class,
        reason=failure_class.value,
        error=str(exc),
        details={"attempt_classes": ordered_classes},
    )


def classify_attempt_failure_class(
    attempt_classes: list[str],
    *,
    terminal: bool,
) -> FailureClass:
    """Classify router attempt exception class names into shared taxonomy."""

    if not attempt_classes:
        return FailureClass.FATAL_EXECUTION_ERROR if terminal else FailureClass.TRANSPORT_API_ERROR

    classes = set(attempt_classes)
    last_class = attempt_classes[-1]

    if _is_auth_exception_class(last_class):
        return FailureClass.FATAL_AUTH_OR_CONFIG
    if _is_timeout_exception_class(last_class):
        return FailureClass.TRANSPORT_TIMEOUT
    if _is_rate_limit_exception_class(last_class):
        return FailureClass.TRANSPORT_RATE_LIMIT
    if classes and all(_is_auth_exception_class(name) for name in classes):
        return FailureClass.FATAL_AUTH_OR_CONFIG
    if terminal:
        return FailureClass.FATAL_EXECUTION_ERROR
    return FailureClass.TRANSPORT_API_ERROR


def _is_transport_timeout_error(exc: Exception) -> bool:
    return any(
        isinstance(current, httpx.TimeoutException)
        or _is_timeout_exception_class(_exception_name(current))
        for current in _iter_exception_chain(exc)
    )


def _is_transport_rate_limit_error(exc: Exception) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, litellm.RateLimitError):
            return True
        if isinstance(current, pydantic_ai.exceptions.ModelHTTPError):
            if getattr(current, "status_code", None) == 429:
                return True
    return False


def _is_transport_api_error(exc: Exception) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(
            current,
            (
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                litellm.APIConnectionError,
                litellm.ServiceUnavailableError,
            ),
        ):
            return True
        if isinstance(current, pydantic_ai.exceptions.ModelHTTPError):
            status_code = getattr(current, "status_code", None)
            if isinstance(status_code, int) and status_code >= 500:
                return True
    return False


def _is_fatal_auth_or_config_error(exc: Exception) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, litellm.AuthenticationError):
            return True
        if isinstance(current, pydantic_ai.exceptions.ModelHTTPError):
            status_code = getattr(current, "status_code", None)
            if status_code in {401, 403}:
                return True
    return False


def _exception_name(exc: Exception) -> str:
    return type(exc).__name__


def _iter_exception_chain(exc: Exception) -> list[Exception]:
    queue: list[BaseException] = [exc]
    seen: set[int] = set()
    ordered: list[Exception] = []

    while queue:
        current = queue.pop(0)
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)

        if isinstance(current, Exception):
            ordered.append(current)

        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if isinstance(cause, BaseException):
            queue.append(cause)
        if isinstance(context, BaseException):
            queue.append(context)

    return ordered


def _is_timeout_exception_class(name: str) -> bool:
    return name in {"RouterChainTimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"}


def _is_rate_limit_exception_class(name: str) -> bool:
    return "RateLimit" in name


def _is_auth_exception_class(name: str) -> bool:
    return name in {"AuthenticationError", "AuthError"} or "Authentication" in name


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sanitize_error_detail_string(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub(_redact_secret_match, sanitized)

    if len(sanitized) > _MAX_ERROR_DETAIL_STRING_LENGTH:
        sanitized = sanitized[: _MAX_ERROR_DETAIL_STRING_LENGTH - 3] + "..."
    return sanitized


def _redact_secret_match(match: re.Match[str]) -> str:
    if match.re.pattern.startswith("\\b(sk-"):
        return "sk-[REDACTED]"
    if len(match.groups()) >= 2:
        return f"{match.group(1)}[REDACTED]"
    return "[REDACTED]"


__all__ = [
    "ClassifiedFailure",
    "EvidenceConfidence",
    "ExecutorFailureEvidence",
    "FailureClass",
    "NormalizationResult",
    "RecoveryAttempt",
    "RecoveryTrace",
    "RetryDecision",
    "RetryMode",
    "build_retry_feedback",
    "build_recovery_error_details",
    "classify_attempt_failure_class",
    "classify_empty_output",
    "classify_output_failure",
    "decide_retry",
    "merge_recovery_error_details",
    "normalize_llm_json_text",
    "sanitize_error_details",
]
