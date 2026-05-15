# Task 1: Retry config + AnalysisOutput contract evolution

## Context

Read: `overview.md`, `2-plan.md` §2, §3.

This task lays the foundation for Phase 1 by introducing retry policy config and removing the current contract conflict where `retry_count` is policy-like in runtime but hardcoded in `AnalysisOutput`.

## Files

- Modify: `src/secondsight/config/schema.py`
- Modify: `src/secondsight/config/loader.py`
- Modify: `src/secondsight/analysis/output.py`
- Test: `tests/analysis/test_output_contract.py`
- Test: `tests/config/test_*`

## Death Test Requirements

- Test: `[analysis.retry].output_repair_max_attempts = -1` is rejected
- Test: `[analysis.retry].output_repair_max_attempts` above hard cap is rejected
- Test: `AnalysisOutput.retry_count` accepts values through the new hard cap
- Test: old `retry_count=3` death test is updated to the new contract boundary rather than stale `<= 2`

## Acceptance Criteria

- Retry policy is config-driven
- `AnalysisOutput` no longer hardcodes the runtime retry budget to 2
