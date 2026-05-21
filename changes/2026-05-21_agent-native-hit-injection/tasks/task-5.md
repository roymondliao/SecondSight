# Task 5: BehaviorFlag.confidence dual-role documentation

## Context

Read: `../overview.md` and `../2-plan.md` (specifically the
gap_6 resolution in §1.5 Pre-thinking).

The invariant #4 of `../1-kickoff.md` says analysis must include
"LLM double-check review to discriminate correct behaviour from
distribution-shift drift". During Planning Step 1.5, we resolved
this by REUSING the existing `BehaviorFlag.confidence` field
(`src/secondsight/analysis/schemas.py:100`) rather than building
a new LLM pass.

This task is purely documentary: it adds a docstring or inline
comment to `BehaviorFlag.confidence` naming this dual role, so
future readers understand why the field exists and what
operational signal it carries. No new code, no schema change,
no tests required.

The reason this is a separate task rather than a comment in
task-2 or task-3: this documentation is not about the hit
injection wrapper itself — it's about a pre-existing field in
the analysis schema that now serves an additional named role.
Keeping it separate makes the change inspectable in git history
("when did confidence become a contamination tripwire?" → this
commit).

## Files

- Modify: `src/secondsight/analysis/schemas.py` — add or extend
  docstring on `BehaviorFlag.confidence` field (around line 100).

## Death Test Requirements

This task has no death tests — it is documentation. The
acceptance is verified by reading the file.

Verification:
- The docstring or inline comment near `confidence: Literal["high", "medium", "low"]` must explicitly mention:
  - (a) LLM's self-assessment of how confident it is in the flag.
  - (b) Operational role as the tripwire for distribution-shift contamination per invariant #4 of `changes/2026-05-21_agent-native-hit-injection/1-kickoff.md`.

## Implementation Steps

- [ ] Step 1: Read `src/secondsight/analysis/schemas.py` around line 82-101 to find the existing `BehaviorFlag.confidence` field definition and its surrounding docstring.
- [ ] Step 2: Extend (or add) docstring text. Suggested wording:
  ```python
  confidence: Literal["high", "medium", "low"]
  """LLM self-assessment of this flag's reliability.

  Dual role:
  - (a) Quality signal for the flag itself; downstream filters may
    elect to ignore "low" confidence flags.
  - (b) Tripwire for distribution-shift contamination — a
    sustained spike in "low" confidence ratio across many sessions
    is the operational signal that meta-injection wrapper artefacts
    may be polluting the analysis pipeline. See invariant #4 in
    changes/2026-05-21_agent-native-hit-injection/1-kickoff.md.
  """
  ```
  (Adjust to project's existing docstring style; pydantic
  `Field(description=...)` is also acceptable if that is the
  prevailing pattern.)
- [ ] Step 3: Verify the new comment is visible via `grep` and the file still parses (`python -c "from secondsight.analysis.schemas import BehaviorFlag; print(BehaviorFlag.model_fields['confidence'])"`).
- [ ] Step 4: Run existing tests to confirm no regression (`rtk proxy pytest tests/analysis/ -q`).
- [ ] Step 5: Write scar report (will be short — this is a doc-only change).
- [ ] Step 6: Commit.

## Expected Scar Report Items

- Potential shortcut: writing a vague comment like "this field also serves as contamination tripwire" without naming WHERE the invariant is documented. The docstring must reference `changes/2026-05-21_agent-native-hit-injection/1-kickoff.md` so future readers can find the framing.
- Assumption to verify: that the project's existing docstring style accepts multi-line docstrings on pydantic fields. If pydantic Field(description=...) is the only sanctioned form, adapt accordingly.

## Acceptance Criteria

- Covers: "BehaviorFlag.confidence remains undocumented as contamination tripwire" (death path) — directly resolved by adding the comment.
