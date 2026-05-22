# Task 5: Enforce capacity ceiling and expose lifecycle state to operator surfaces

## Context

Read: `overview.md`, `2-plan.md` sections "Component #4 — Operator visibility"
and "Component #5 — Capacity ceiling".

This task finishes the operator-facing contract and project-level active-set
bounding. Prompt selection order must remain frequency-based.

## Files

- Modify: `src/secondsight/config/schema.py`
- Modify: `src/secondsight/config/loader.py`
- Modify: `src/secondsight/config/template.py`
- Modify: `src/secondsight/api/directives.py`
- Modify: `src/secondsight/cli/directive.py`
- Modify: `src/secondsight/feedback/convention.py`
- Modify: `src/secondsight/feedback/lifecycle_automation.py`
- Modify: `src/secondsight/storage/directives_repository.py`
- Modify: `tests/cli/test_directive.py`
- Modify: `tests/storage/test_directives_repository.py`
- Modify: `tests/analysis/test_orchestrator.py`

## Death Test Requirements

- Test: `active=false` directive listings include `obsolete` and `stalled`
  rows with policy metadata fields
- Test: capacity shedding uses lowest weight, not highest frequency
- Test: convention selection order remains frequency-based even after weight
  data exists

## Implementation Steps

- [ ] Step 1: Write death tests
- [ ] Step 2: Run death tests — verify they fail
- [ ] Step 3: Write unit tests
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement minimal code to pass all tests
- [ ] Step 6: Run all tests — verify they pass
- [ ] Step 7: Write scar report
- [ ] Step 8: Commit

## Expected Scar Report Items

- Potential shortcut: exposing new lifecycle fields only for some statuses,
  causing shape drift in API/CLI contracts
- Potential shortcut: reusing frequency order for shedding because the query
  path already has that sort
- Assumption to verify: operator surfaces need dormant/system-owned states for
  debugging autonomous lifecycle, not just active/disabled

## Acceptance Criteria

- Covers: "capacity shedding uses frequency instead of weight"
- Covers: "operator view hides obsolete and stalled states"
- Covers: "global active convention ceiling stays bounded"
