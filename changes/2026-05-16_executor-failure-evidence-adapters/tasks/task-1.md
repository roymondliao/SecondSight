# Task 1: Add executor failure evidence contract to shared recovery

## Context

Read: `overview.md`, `2-plan.md` §§1-2, §5, and acceptance scenarios DC2/DC4/degradation.

This task creates the internal evidence contract and teaches shared recovery how to consume stable evidence without changing existing retry policy semantics.

## Files

- Modify: `src/secondsight/analysis/output_recovery.py`
- Test: `tests/analysis/test_output_recovery.py`

## Death Test Requirements

- Test: adapter evidence with `failure_class=fatal_auth_or_config` produces `ClassifiedFailure.failure_class=fatal_auth_or_config` and preserves `evidence_source`, `evidence_confidence`, and `evidence_executor` in details.
- Test: evidence raw details containing `reason`, `failure_class`, `retry_mode`, or `attempts` cannot overwrite the shared `build_recovery_error_details()` envelope and are preserved under `raw_error_details`.
- Test: existing direct exception classification for JSON/Pydantic/timeout/provider typed exceptions remains unchanged when no evidence is supplied.
- Test: an evidence object with no usable class/reason becomes `fatal_execution_error` with low-confidence metadata, not a guessed transport/auth class.

## Implementation Steps

- [ ] Step 1: Write death tests.
- [ ] Step 2: Run death tests — verify they fail.
- [ ] Step 3: Add `EvidenceConfidence` and `ExecutorFailureEvidence` internal models.
- [ ] Step 4: Add evidence-aware classification entry point while keeping `classify_output_failure(exc)` backward compatible.
- [ ] Step 5: Ensure evidence metadata flows through `ClassifiedFailure.details` and existing sanitizer.
- [ ] Step 6: Run targeted tests.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- Potential shortcut: keeping auth/config raw message marker lists in shared recovery and merely wrapping their output as evidence.
- Assumption to verify: evidence fields remain internal and do not require `AnalysisOutput` schema changes.
- Assumption to verify: sanitizer still redacts and bounds nested raw evidence.

## Acceptance Criteria

- Covers: `DC2 — ambiguous CLI failure stays low confidence`
- Covers: `DC4 — evidence metadata cannot overwrite shared recovery envelope`
- Covers: `Degradation — legacy typed exception classification still works without explicit evidence`
