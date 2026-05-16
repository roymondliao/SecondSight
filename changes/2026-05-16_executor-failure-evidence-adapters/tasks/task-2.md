# Task 2: Move CLI raw failure interpretation into CLI adapters

## Context

Read: `overview.md`, `2-plan.md` §§3, 5-6, and acceptance scenarios DC1/DC2/happy path.

This task makes Claude/Codex CLI failure classification adapter-owned. The CLI dispatcher should ask adapters for evidence and pass that evidence into shared recovery.

## Files

- Modify: `src/secondsight/analysis/cli_adapters/claude_code.py`
- Modify: `src/secondsight/analysis/cli_adapters/codex.py`
- Modify: `src/secondsight/analysis/cli_dispatcher.py`
- Test: `tests/analysis/test_cli_adapters.py`
- Test: `tests/analysis/test_cli_dispatcher.py`

## Death Test Requirements

- Test: Claude non-zero structured stdout with auth/config evidence is classified as `fatal_auth_or_config` through adapter evidence, with `evidence_source=cli_stdout_envelope`, `evidence_confidence=derived` or `heuristic`, and `evidence_executor=claude_code`.
- Test: Claude non-zero structured stdout with ambiguous error text becomes `fatal_execution_error`, not transport/auth, and records low-confidence evidence metadata.
- Test: Codex output file read failure emits adapter evidence with `source=cli_output_file`, `executor=codex`, and `failure_class=fatal_execution_error`.
- Test: CLI raw details colliding with shared envelope keys are preserved under `raw_error_details`.

## Implementation Steps

- [ ] Step 1: Write death tests.
- [ ] Step 2: Run death tests — verify they fail.
- [ ] Step 3: Add Claude adapter evidence extraction helper for non-zero stdout/stderr context.
- [ ] Step 4: Add Codex adapter evidence helper for output-file failures.
- [ ] Step 5: Update CLI dispatcher non-zero and output-file failure paths to pass `ExecutorFailureEvidence` into shared recovery.
- [ ] Step 6: Remove or neutralize CLI/provider message marker ownership from shared recovery for CLI paths.
- [ ] Step 7: Run targeted CLI tests.
- [ ] Step 8: Write scar report.
- [ ] Step 9: Commit.

## Expected Scar Report Items

- Potential shortcut: leaving `_extract_claude_failure_context()` in dispatcher as the de facto adapter.
- Potential shortcut: classifying ambiguous CLI output as auth/rate-limit because text "looks like" one provider's message.
- Assumption to verify: Codex empty model output remains output-repair classification, not CLI execution evidence.

## Acceptance Criteria

- Covers: `DC1 — shared classifier does not own CLI message markers`
- Covers: `DC2 — ambiguous CLI failure stays low confidence`
- Covers: `Happy path — adapter-derived Claude auth failure is classified consistently`
