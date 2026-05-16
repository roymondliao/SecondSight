# Task 2: SDK dispatcher adoption

## Context

Read: `overview.md`, `2-plan.md` §3.

This task applies the shared recovery contract to SDK mode without collapsing SDK into CLI semantics.

## Files

- Modify: `src/secondsight/analysis/sdk_dispatcher.py`
- Modify: `tests/analysis/test_sdk_dispatcher.py`

## Death Test Requirements

- Test: structured-output validation failure enters output-repair retry
- Test: provider auth/config failure is no-retry
- Test: transport timeout uses transport classification, not schema feedback
