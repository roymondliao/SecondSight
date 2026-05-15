# Task 3: Harden Codex hook installation shape and fixture regeneration rules

## Context
Read: `overview.md`

Real local capture proved that a working Codex tool-hook setup uses explicit `PreToolUse` / `PostToolUse` registrations and matcher-aware entries. This task makes the installer and its tests reflect that working shape, and leaves maintainers with load-bearing fixture-refresh rules so the next refresh does not drift back to invented contracts.

## Files
- Modify: `src/secondsight/installer/codex_hooks.py:1-220`
- Modify: `tests/installer/test_codex_hooks.py:1-139`
- Create: `tests/fixtures/codex/_README.md`
- Test: `tests/installer/test_codex_hooks.py`

## Death Test Requirements
- Test: fresh install must contain all five Codex observation hooks, including `PreToolUse`.
- Test: tool-hook entries must match the verified working registration shape; a partial or ambiguous registration must fail the installer tests.
- Test: existing user hooks and foreign SecondSight installs must remain preserved/detected rather than overwritten silently.

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
- Potential shortcut: assuming Codex tool hooks work without matching the verified local registration shape.
- Potential shortcut: documenting capture provenance only in change notes and not in a fixture-local README.
- Assumption to verify: installer output should align to the verified working local configuration even when upstream docs are vague.

## Acceptance Criteria
- Covers: `Degradation - Codex tool hooks are registered in a shape that does not actually fire`
- Covers: `Success - verified Codex hook payloads survive the observation path with evidence`
