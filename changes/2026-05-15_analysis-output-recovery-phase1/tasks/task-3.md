# Task 3: CLI dispatcher integration + validation suite

## Context

Read: `overview.md`, `2-plan.md` §4, acceptance scenarios DC1-DC4.

This task wires Phase 1 into the existing CLI dispatcher and proves that normalizable output no longer consumes retry budget.

## Files

- Modify: `src/secondsight/analysis/cli_dispatcher.py`
- Modify: `tests/analysis/test_cli_dispatcher.py`

## Death Test Requirements

- Test: fenced JSON returns success with `retry_count=0`
- Test: malformed JSON still retries and fails within policy budget
- Test: schema mismatch uses structured feedback rather than raw exception dump
- Test: clean valid JSON remains a no-op path

## Acceptance Criteria

- CLI dispatcher uses Phase 1 helpers end-to-end
- Existing non-output subprocess failure handling remains intact
