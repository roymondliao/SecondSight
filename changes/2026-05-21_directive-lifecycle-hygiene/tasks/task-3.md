# Task 3: Add deterministic weight policy and replace flag-type rebound lifecycle logic

## Context

Read: `overview.md`, `2-plan.md` sections "Component #2 — Weight policy layer"
and "Component #3 — Aggregator / lifecycle integration".

This task owns the policy seam. `weight` is a policy memory signal, not a
derived formula and not a prompt ordering score.

## Files

- Create: `src/secondsight/feedback/directive_policy.py`
- Modify: `src/secondsight/feedback/lifecycle.py`
- Modify: `src/secondsight/feedback/lifecycle_automation.py`
- Modify: `src/secondsight/analysis/aggregator.py`
- Modify: `src/secondsight/storage/directives_repository.py`
- Create: `tests/feedback/test_directive_policy.py`
- Modify: `tests/feedback/test_lifecycle.py`
- Modify: `tests/storage/test_directives_repository.py`

## Death Test Requirements

- Test: `source_flag_seen=True` and `same_identity_repromoted=False` does not
  decay weight, but does mark for revision/stalled handling
- Test: disabled directives are excluded from policy evaluation
- Test: old `source_flag_type` lookback revival logic is gone and does not
  independently reactivate obsolete rows

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

- Potential shortcut: encoding weight as a direct function of frequency
- Potential shortcut: letting policy update prompt order by changing
  `get_active_conventions()` sort semantics
- Assumption to verify: lifecycle module should remain the single transition
  validator even after new statuses are added

## Acceptance Criteria

- Covers: "source flag family still exists but policy decays instead of revising"
- Covers: "disabled directives are pulled back into autonomous lifecycle"
- Covers: "successful teaching fades a convention into obsolete"
