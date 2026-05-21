# Task 1: Migration audit + scope lock (no implementation)

## Context

Read: `../overview.md` and `../2-plan.md`.

This task does NOT write production code. It verifies that the
file lists in `overview.md` and `2-plan.md` are still accurate
against the current `main` branch, and produces a frozen audit
artifact that subsequent tasks can rely on without re-running grep.

The purpose is to (a) catch any drift between research-time audit
and implementation-time reality, (b) lock the scope of the
atomic destructive change in task-4, and (c) record a reproducible
audit trail.

## Files

- Create: `changes/2026-05-21_agent-native-hit-injection/migration-audit.yaml`
  (a frozen snapshot of references to deleted symbols, with line
  numbers; consumed by task-4 as ground truth)

No source modifications in this task.

## Death Test Requirements

This task has no death tests because it produces no executable
artifact. Instead, it produces a verified audit document.

Verification (not death test):
- `grep -rln "prompt_evaluator\|/hook/injection/user-prompt" src/ tests/ scripts/ --include='*.py' --include='*.sh' --include='*.toml'`
  output must exactly match the file list in `migration-audit.yaml`.

## Implementation Steps

- [ ] Step 1: Run `grep -rln "prompt_evaluator\|/hook/injection/user-prompt" src/ tests/ scripts/ --include='*.py' --include='*.sh' --include='*.toml'` from repository root.
- [ ] Step 2: For each file matched, record the line numbers where the symbol appears (`grep -n` per file).
- [ ] Step 3: Categorise each reference as one of: `delete-entire-file`, `surgical-remove`, `audit-only`. Categorisation rules:
  - `delete-entire-file`: the whole file's purpose is the deleted code (e.g., `tests/feedback/test_prompt_evaluator.py`).
  - `surgical-remove`: a section of the file references the deleted code and must be excised (e.g., `src/secondsight/api/injection.py`).
  - `audit-only`: appears in a comment or unrelated test (likely none, but possible).
- [ ] Step 4: Write `migration-audit.yaml` with the categorised result; include the exact grep command used and the repo HEAD SHA at audit time.
- [ ] Step 5: Run grep again to confirm reproducibility; SHA must still match.
- [ ] Step 6: Write scar report — what surprised the audit (any unexpected hits not predicted by `overview.md`)?
- [ ] Step 7: Commit.

## Expected Scar Report Items

- Potential shortcut: skipping the per-file `grep -n` (line numbers) because they're tedious. Don't skip — task-4 needs them.
- Assumption to verify: `overview.md` claims 5 files match. If audit returns different count, document the delta and flag for re-planning.
- Assumption to verify: that no `prompt_evaluator` references exist outside `src/` and `tests/` (e.g., in docs, in `.claude/`). If any are found, decide in `migration-audit.yaml` whether they need follow-up.

## Acceptance Criteria

- Covers: "atomic deletion leaves dangling import or route reference" (death path) — task-1's audit is the input that task-4 uses to be exhaustive.

## Notes

This task is intentionally small and bureaucratic. Its purpose is
to make task-4's destructive change unambiguous and verifiable.
If task-4 ever fails because of a missed reference, task-1 has
failed its function — record that as a scar.

## Planning Amendment (2026-05-21)

The grep scope in Step 1 was originally written as `src/ tests/`. This was
too narrow: `scripts/` contains executable shell hooks (`scripts/hooks/*.sh`)
that call the API server at runtime and must be included in any audit of live
references to deleted endpoints.

The original scope omission caused `scripts/hooks/user-prompt.sh` line 118 to
be invisible to the audit. That line is a live curl call to the deleted route;
if task-4 had removed the API endpoint without removing this curl call, the
hook would have silently failed (curl non-2xx → `|| return 0` → no injection,
no error logged to the user).

**Root cause:** the Step 1 grep command was written from knowledge of the
Python source tree without checking what other executable directories existed
at the repo root. The correct approach is to first enumerate top-level
executable directories:

    find . -maxdepth 1 -type d | sort

...then include all directories that contain runtime-executed code (src/,
tests/, scripts/, and any others found). Documentation directories (docs/,
changes/, bugfix/, .agents/, .github/) do not need to be in the primary scope
but should be covered in the extended scope audit.

This amendment corrects the grep command in Step 1 and the matching
Verification command above. The `migration-audit.yaml` has been updated to
reflect the corrected scope and the newly found 6th file.
