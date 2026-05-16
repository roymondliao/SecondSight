# Task 3: Add SDK/router evidence extraction and parity validation

## Context

Read: `overview.md`, `2-plan.md` §§4-6, and acceptance scenario DC3.

This task makes SDK classification evidence explicit. SDK/router should prefer attempt records, exception chains, provider status codes, and validation errors over message string parsing.

## Files

- Modify: `src/secondsight/analysis/sdk_dispatcher.py`
- Modify: `src/secondsight/analysis/output_recovery.py`
- Test: `tests/analysis/test_sdk_dispatcher.py`
- Test: `tests/analysis/test_output_recovery.py`

## Death Test Requirements

- Test: `RouterChainExhaustedError` attempt records with typed auth/rate-limit/timeout classes classify from attempt evidence even when the outer exception message is misleading.
- Test: `RouterTerminalError.attempts` preserves fallback attribution and evidence metadata when terminal failure happens after fallback.
- Test: SDK validation failures still classify as `schema_mismatch` or `json_decode` and use output-repair retry semantics.
- Test: SDK fallback or provider raw details cannot overwrite shared recovery envelope keys.

## Implementation Steps

- [ ] Step 1: Write death tests.
- [ ] Step 2: Run death tests — verify they fail.
- [ ] Step 3: Add SDK evidence extraction helper from router exceptions and exception chains.
- [ ] Step 4: Route SDK dispatch failures through evidence-aware shared classification.
- [ ] Step 5: Preserve validation-error-specific output-repair behavior.
- [ ] Step 6: Run targeted SDK/shared tests.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- Potential shortcut: using `str(exc)` to classify router failures when attempt classes are available.
- Potential shortcut: treating all terminal router errors as fatal auth/config without verifying typed/status evidence.
- Assumption to verify: SDK evidence metadata does not change public `AnalysisOutput` schema.

## Acceptance Criteria

- Covers: `DC3 — SDK typed attempt evidence beats misleading message text`
- Covers: `Degradation — legacy typed exception classification still works without explicit evidence`
