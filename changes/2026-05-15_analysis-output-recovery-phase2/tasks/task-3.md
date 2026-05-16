# Task 3: Unified observability and error taxonomy

## Context

Read: `overview.md`, `2-plan.md` §4.

This task makes the shared recovery layer operationally useful by aligning CLI and SDK logs / `error_details` semantics.

## Files

- Modify: `src/secondsight/analysis/cli_dispatcher.py`
- Modify: `src/secondsight/analysis/sdk_dispatcher.py`
- Test: CLI/SDK dispatcher tests

## Death Test Requirements

- Test: same failure class yields same `error_details["failure_class"]` in both modes
- Test: retry exhaustion is reported consistently
- Test: mode-specific raw evidence is preserved without breaking shared taxonomy
