# Task 4: Atomic destructive change — delete sidecar + endpoint + obsolete tests

## Context

Read: `../overview.md`, `../2-plan.md`, and the audit artifact at
`../migration-audit.yaml` (produced by task-1).

After task-2 (new wrapper) and task-3 (hook script wired to new
wrapper) have shipped, the legacy sidecar pathway is dead code
under normal operation but the files still exist. This task
removes them atomically. Per Q1 framing ("no hedging"), there is
no staged rollout: this task's commit is the moment the old
pathway ceases to exist in the codebase.

The destructive scope is fully enumerated in `migration-audit.yaml`.
The implementer must NOT discover new files to delete in this
task — if `migration-audit.yaml` is incomplete, that is a task-1
failure; stop and go back.

## Files

### Delete entire file

- `src/secondsight/feedback/prompt_evaluator.py`
- `src/secondsight/prompts/feedback/classifier.jinja2`
- `src/secondsight/prompts/feedback/guidance/missing_target.jinja2`
- `src/secondsight/prompts/feedback/guidance/missing_scope.jinja2`
- `src/secondsight/prompts/feedback/guidance/missing_success_criteria.jinja2`
- `src/secondsight/prompts/feedback/guidance/multiple_interpretations.jinja2`
- `src/secondsight/prompts/feedback/guidance/__init__.py`
- `src/secondsight/prompts/feedback/guidance/` (empty directory; remove)
- `tests/feedback/test_prompt_evaluator.py` (Layer 3 RED death test goes with the file)
- `tests/api/test_injection_user_prompt.py`

### Surgically remove from file

- `src/secondsight/api/injection.py` — remove the
  `/hook/injection/user-prompt/{agent}` route registration plus
  its handler function and any associated imports of
  `prompt_evaluator` that become unused.
- `scripts/hooks/user-prompt.sh` — remove the legacy curl-to-
  injection-endpoint block that task-3 left intact (everything
  from the `local injection_payload="$(curl ...` line through the
  `[ -n "$injection_payload" ] && printf` line, and any cleanup
  of related logging variables that become orphaned).

### Audit-only (no expected change)

- `tests/api/test_injection_session_start.py` — verify no
  dependency on deleted symbols.

## Death Test Requirements

**DT-8: No surviving reference to `prompt_evaluator` or the deleted endpoint URL.**
Test name: `test_dt_no_dangling_references_to_deleted_sidecar`
Given: this task's commit is checked out
When:  `grep -rln "prompt_evaluator\|/hook/injection/user-prompt" src/ tests/ --include='*.py' --include='*.sh' --include='*.toml'` is run
Then:  command returns empty (zero matches).

**DT-9: Deleted endpoint returns 404 in HTTP integration test.**
Test name: `test_dt_deleted_endpoint_returns_404`
Given: SecondSight API server is running with the post-task-4 codebase
When:  POST `/hook/injection/user-prompt/claude_code` with any payload
Then:  HTTP 404 (not 5xx — the route truly doesn't exist, not
       broken handler).

**DT-10: Hook script post-deletion still produces wrapper output for valid prompts.**
Test name: `test_dt_hook_still_works_after_atomic_deletion`
Given: this task's commit is checked out; config enabled; valid stdin
When:  `scripts/hooks/user-prompt.sh` is executed
Then:  stdout contains the new wrapper text (from task-2's
       template). Confirms the legacy block was removed without
       breaking the new path.

**DT-11: `~/.secondsight/logs/curl-errors.log` does not gain new injection-endpoint timeout entries.**
Test name: `test_dt_curl_errors_log_does_not_gain_injection_entries_after_deletion`
Given: this task's commit is checked out; clean log file; hook is exercised 10 times with diverse stdin
When:  after 10 invocations, the log is inspected
Then:  no new entries of the form `curl error: ... /hook/injection/user-prompt/`
       (because that path no longer exists in any code path).

## Implementation Steps

- [ ] Step 1: Re-read `../migration-audit.yaml` and verify it matches the current grep state. If drift, STOP and re-do task-1.
- [ ] Step 2: Write DT-8, DT-9, DT-10, DT-11.
- [ ] Step 3: Run tests — verify they fail (DT-8 should fail because references still exist; DT-9 should fail because endpoint still serves).
- [ ] Step 4: Delete files per the "Delete entire file" list.
- [ ] Step 5: Surgical removal in `src/secondsight/api/injection.py` — find the `/hook/injection/user-prompt/{agent}` route and excise; also remove now-unused imports of `prompt_evaluator`.
- [ ] Step 6: Surgical removal in `scripts/hooks/user-prompt.sh` — remove the legacy curl block (keep the early-exit and Python invocation from task-3).
- [ ] Step 7: Audit `tests/api/test_injection_session_start.py` for any incidental imports of `prompt_evaluator`; if found, remove. If not, leave alone.
- [ ] Step 8: Run full test suite. Expect: DT-8 through DT-11 pass; existing SessionStart tests still pass; no test imports `prompt_evaluator`.
- [ ] Step 9: Run pre-commit hooks (`rtk pre-commit run --all-files` or equivalent) — flag any toml-sort / ruff / mypy issues introduced by removed imports.
- [ ] Step 10: Write scar report — what was harder to delete than expected? Were there hidden coupling sites?
- [ ] Step 11: Commit. Commit message should explicitly note: "atomic destructive change per changes/2026-05-21_agent-native-hit-injection/2-plan.md task-4; sidecar classifier removed."

## Expected Scar Report Items

- Potential shortcut: deleting files without running DT-8 first to confirm references are also gone. The audit may have missed a non-grep-detectable reference (e.g., a string-built endpoint URL via f-string).
- Potential shortcut: leaving "TODO: remove later" comments next to the deletion sites. Don't — atomic means complete.
- Assumption to verify: that no external tool (dashboard frontend, monitoring config, ops script outside the repo) hits `/hook/injection/user-prompt/{agent}`. Out of scope for this task to find, but document any such known integrations in the scar report so future ops issues can be diagnosed.
- Assumption to verify: that the hook script's early-exit + Python invocation from task-3 covers ALL prompts that previously went through the curl block. If task-3 has a code path that conditionally falls through to the old block, that path must be removed in this task.

## Acceptance Criteria

- Covers: "atomic deletion leaves dangling import or route reference" (death path) — directly tested via DT-8.
- Covers: "hook script invokes deleted endpoint via legacy curl block" (death path) — directly tested via DT-10 and DT-11.
- Covers: contributes to "enabled, simple prompt, all components present" (happy path) by removing the noise of the legacy path.

## One-shot Risk Acknowledgment

This task is irreversible without `git revert`. The implementer
should commit it as a single coherent commit (not multiple small
ones) so revert is unambiguous if integration tests reveal a
post-deletion regression that planning did not anticipate.
