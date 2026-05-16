# Plan: executor-failure-evidence-adapters

## 1. Architecture

This change inserts an internal evidence boundary before shared recovery classification:

```text
executor raw failure
  -> executor-local evidence adapter
  -> shared evidence-aware classifier
  -> shared retry decision
  -> shared observability envelope
```

The important boundary is ownership, not naming:

- CLI adapters may inspect process exit code, stdout/stderr, Claude JSON envelopes, Codex output-file conditions, and CLI-specific wording.
- SDK/router may inspect controlled framework exceptions, provider exception classes, HTTP status codes, and router attempt records.
- Shared recovery may classify stable evidence fields and typed exceptions, but must not own CLI/provider message marker lists.

## 2. Evidence Contract

Add an internal typed evidence model in `src/secondsight/analysis/output_recovery.py`.

Required output states:

- `success`: evidence confidently maps to a `FailureClass`.
- `failure`: raw failure existed but adapter could not derive a specific stable class; classify as `fatal_execution_error` with low-confidence evidence.
- `unknown`: evidence is absent or internally contradictory; classify loud with `fatal_execution_error` and preserve sanitized raw source metadata.

Initial fields:

- `source`: stable origin such as `cli_exit`, `cli_stdout_envelope`, `cli_output_file`, `sdk_exception`, `sdk_router_attempt_trace`, `output_validation`.
- `executor`: `claude_code`, `codex`, `sdk`, or `unknown`.
- `failure_class`: optional `FailureClass` proposed by the adapter.
- `reason`: optional stable machine reason.
- `message`: sanitized bounded diagnostic string.
- `raw`: sanitized JSON-safe adapter-local details.
- `confidence`: `typed`, `derived`, `heuristic`, or `unknown`.

Shared classification rules:

- If evidence has a valid `failure_class`, trust it and preserve source/confidence in `ClassifiedFailure.details`.
- If no evidence is supplied, keep existing typed-exception classification behavior for JSON/Pydantic/transport exceptions.
- Message-marker fallback may exist only behind adapter-local evidence extraction, and must be recorded as `confidence=heuristic`.

## 3. CLI Evidence Adapters

Move CLI/provider raw text knowledge out of shared recovery.

Claude Code:

- Parse non-zero stdout JSON envelope in adapter-owned helper.
- Preserve `api_error_status`, `subtype`, `is_error`, and bounded result message.
- Map 401/403/auth marker evidence to `fatal_auth_or_config`.
- Map 429/quota/rate-limit evidence to `transport_rate_limit` only when adapter evidence is explicit enough.
- Otherwise emit `fatal_execution_error` with source/confidence metadata.

Codex:

- Treat unreadable/missing output file as `fatal_execution_error` with `source=cli_output_file`.
- Treat empty output as output-validation evidence, not CLI transport evidence.
- Keep Codex-specific command/output-file conventions in `cli_adapters/codex.py` or CLI dispatcher-local adapter glue, not shared recovery.

## 4. SDK Evidence Adapter

SDK evidence should primarily come from controlled types:

- `RouterChainExhaustedError.attempts`
- `RouterTerminalError.attempts`
- exception chain classes and typed HTTP/status attributes
- Pydantic validation errors

SDK message string fallback is allowed only when no typed/status evidence exists, and it must be recorded as `confidence=heuristic`.

## 5. Observability Contract

The existing shared envelope remains stable:

- `reason`
- `failure_class`
- `attempts`
- `retry_exhausted`
- `retry_mode`
- `recovery_trace`

Add evidence metadata under `error_details` without overwriting shared keys:

- `evidence_source`
- `evidence_confidence`
- `evidence_executor`
- `evidence_reason`
- `raw_error_details` for collisions or additional forensics

All evidence serialized into `error_details` must pass the existing redaction and string-bound sanitizer.

## 6. Death Cases

### DC1: Shared Classifier Learns CLI Wording Again

Trigger:
A new provider/CLI message marker is added directly to `output_recovery.py`.

The lie:
The taxonomy is shared and executor-agnostic.

The truth:
Adapter quirks leaked into shared policy again.

How to detect:
Tests assert CLI marker strings are classified through adapter evidence, not by calling `classify_output_failure(Exception("api key ..."))`.

### DC2: Adapter Emits Raw Evidence Without Confidence

Trigger:
CLI non-zero exit creates `error_details` from raw stderr/stdout but omits source/confidence.

The lie:
Operators see a normalized `failure_class`.

The truth:
They cannot tell whether the classification came from typed data, derived status, or heuristic text.

How to detect:
Death tests require `evidence_source`, `evidence_confidence`, and `evidence_executor` on adapter-derived classifications.

### DC3: SDK Typed Evidence Regresses to Message Parsing

Trigger:
Router attempt records with typed `AuthenticationError` or `RateLimitError` are classified by string matching the outer exception message.

The lie:
SDK classification works.

The truth:
Provider wording drift can silently change classification.

How to detect:
Tests use attempt records with misleading outer messages and assert typed attempt class wins.

### DC4: Low-Confidence Evidence Looks Certain

Trigger:
An unknown non-zero CLI failure is mapped to a specific transport/auth class from weak text.

The lie:
Retry/failure policy appears precise.

The truth:
The adapter guessed from ambiguous evidence.

How to detect:
Ambiguous CLI output must produce `fatal_execution_error` with `confidence=unknown` or `heuristic`, not a transport retry class.

## 7. File Map

- Modify: `src/secondsight/analysis/output_recovery.py`
- Modify: `src/secondsight/analysis/cli_dispatcher.py`
- Modify: `src/secondsight/analysis/cli_adapters/claude_code.py`
- Modify: `src/secondsight/analysis/cli_adapters/codex.py`
- Modify: `src/secondsight/analysis/sdk_dispatcher.py`
- Test: `tests/analysis/test_output_recovery.py`
- Test: `tests/analysis/test_cli_adapters.py`
- Test: `tests/analysis/test_cli_dispatcher.py`
- Test: `tests/analysis/test_sdk_dispatcher.py`

## 8. Exit Condition

This change is complete when:

- Shared recovery can classify adapter evidence without parsing CLI/provider-specific raw text.
- Claude/Codex/SDK failure paths preserve source/confidence metadata in `error_details`.
- Existing retry behavior remains semantically unchanged.
- Adding a future CLI executor can implement evidence extraction without editing shared marker lists in `output_recovery.py`.
