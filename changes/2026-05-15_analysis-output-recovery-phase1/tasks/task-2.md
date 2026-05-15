# Task 2: Shared output recovery helpers

## Context

Read: `overview.md`, `2-plan.md` §2, §4.

This task creates the shared helper layer that Phase 1 ships and Phase 2 reuses.

## Files

- Create: `src/secondsight/analysis/output_recovery.py`
- Test: `tests/analysis/test_output_recovery.py`

## Death Test Requirements

- Test: fenced JSON normalizes without changing inner JSON
- Test: preface/suffix noise trims to the first top-level object
- Test: malformed JSON cannot be silently "fixed" by the normalizer
- Test: feedback builder output is bounded by `feedback_max_chars`
- Test: classifier distinguishes `json_decode` from `schema_mismatch`

## Acceptance Criteria

- Shared helper layer exists
- No dispatcher-specific subprocess logic leaks into it
