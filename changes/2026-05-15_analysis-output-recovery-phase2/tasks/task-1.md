# Task 1: Expand shared recovery taxonomy and internal models

## Context

Read: `overview.md`, `2-plan.md` §2.

Phase 2 starts by widening Phase 1's helper layer into a true shared internal contract.

## Files

- Modify: `src/secondsight/analysis/output_recovery.py`
- Test: `tests/analysis/test_output_recovery.py`

## Death Test Requirements

- Test: transport timeout is classified separately from json/schema failures
- Test: fatal auth/config is no-retry
- Test: retry decision objects are serializable / inspectable for logs
