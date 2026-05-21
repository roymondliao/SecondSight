# Overview: agent-native-hit-injection

## Goal

Replace the sidecar-classifier hit injection with a meta-injection
wrapper executed by the main agent itself, and atomically remove
the old sidecar path so SecondSight commits to agent-native
feedback architecture without rules-based hedging.

## Architecture

UserPromptSubmit hook (`scripts/hooks/user-prompt.sh`) invokes a
small Python helper (`src/secondsight/feedback/hit_injection.py`)
that loads a single Jinja2 template
(`src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2`)
and renders an executability self-check meta-instruction around
the user's prompt. The rendered text is returned via
`hookSpecificOutput.additionalContext` so the main agent — which
already has full conversation context — performs the
executability evaluation as part of its existing inference call.
No subprocess. No external classifier. No JSON envelope from a
separate LLM. Hit detection becomes a property of the main agent's
own reasoning, not an external sidecar's verdict.

The hook is gated by `[feedback].hit_injection_enabled` in
`config.toml` (default `true`); when disabled, the hook exits 0
with no stdout and no meta-injection is applied. This is the
permanent user-side escape hatch per Q3 cost-transfer mitigation.

The deletion of the old sidecar (`prompt_evaluator.py`, the
`/hook/injection/user-prompt/{agent}` endpoint, the
`classifier.jinja2`, the `guidance/*.jinja2` templates, and all
their tests) is atomic with the new wrapper landing; there is no
staged rollout and no flag-gated coexistence.

## Tech Stack

- Python 3 (>=3.11; matches SecondSight baseline)
- Jinja2 (existing via `secondsight.prompts._loader.render`)
- POSIX shell (bash via `scripts/hooks/user-prompt.sh`)
- TOML config schema (existing `src/secondsight/config/`)

## Key Decisions

- **Wrapper as single template, not categorized**: rejects the
  pre-existing `prompts/feedback/guidance/missing_*.jinja2`
  taxonomy because enumerated vague categories are exactly the
  rules-based pattern Q1 framing rejects. Single
  `hit_injection_wrapper.jinja2` lets the main agent name what's
  missing in its own words.
- **Python wrapper, shell hook**: shell stays the event-handler
  layer (existing role); Python handles wrapper logic so it's
  unit-testable.
- **LLM double-check reuses existing `BehaviorFlag.confidence`**:
  no new LLM pass; the existing three-value confidence field
  already serves the contamination-tripwire role. Operationalised
  via post-ship monitoring of low-confidence ratio.
- **Atomic destructive change, no staged rollout**: per Q1 framing
  ("no hedging"), the old sidecar path is removed in the same
  task as the new wrapper takes over.
- **No regression tests for verify-only existing wiring** (#4,
  #6, #10): operator decision; existing behavior is treated as
  inherited and trusted.
- **Layer 3 RED test deleted with parser**: the test was a Phase B
  tripwire from yesterday's bugfix; with the parser module gone,
  the test has no subject and is removed atomically.

## Death Cases Summary

Top 3 most dangerous silent failure paths (full list in
`acceptance.yaml`):

1. **Wrapper produces malformed JSON, hook silently fails open.**
   User prompt contains characters requiring escape (quotes,
   newlines, backslashes); the rendered wrapper produces invalid
   JSON; Claude Code rejects it; hook exit-0 contract masks the
   failure; no meta-injection occurs; system appears to work.

2. **Atomic deletion leaves a dangling reference.** A test or
   internal module retains an `import secondsight.feedback.prompt_evaluator`
   or a reference to `/hook/injection/user-prompt/{agent}` after
   task-4. Pytest may skip and CI may still pass while a stale
   pathway exists in production code.

3. **Config toggle silently defaults to wrong value.** A
   future config-schema validator changes the loader chain such
   that a missing key returns string `"true"` instead of bool
   `True`; condition logic in the hook script treats string
   `"true"` falsy in shell context; feature appears disabled
   while config file says otherwise.

## File Map

```
NEW
  src/secondsight/feedback/hit_injection.py
  src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2

MODIFIED
  src/secondsight/api/injection.py
  src/secondsight/config/loader.py
  src/secondsight/config/schema.py
  src/secondsight/config/template.py
  scripts/hooks/user-prompt.sh
  tests/scripts/test_user_prompt_hook_injection.py
  src/secondsight/analysis/schemas.py        (docstring update only)

DELETED (atomic, task-4)
  src/secondsight/feedback/prompt_evaluator.py
  src/secondsight/prompts/feedback/classifier.jinja2
  src/secondsight/prompts/feedback/guidance/    (entire directory)
  tests/feedback/test_prompt_evaluator.py
  tests/api/test_injection_user_prompt.py

AUDIT-ONLY (no expected change)
  tests/api/test_injection_session_start.py
```

## Verify-Only Existing Wiring

Per operator decision: no regression tests, audit-only with notes
recorded here. Implementer should read but not modify:

- `src/secondsight/analysis/orchestrator.py:488` — confirm
  `run_lifecycle_automation` runs per session_end with no manual
  step (component #4 verify).
- `src/secondsight/analysis/aggregator.py:217-360` — confirm
  pattern-to-directive UPSERT pipeline has no human approval gate
  (component #6 verify).
- `src/secondsight/analysis/schemas.py:166-167` — confirm
  `Directive.source_flag_type` and `source_sessions` exist on
  schema (component #10 verify).

## Chain Context

- Predecessor: `bugfix/2026-05-20_user-prompt-injection-timeout/`
- Sibling (deferred): `changes/2026-05-21_directive-lifecycle-hygiene/`
- `1-kickoff.md` and `problem-autopsy.md` in this folder are the
  authoritative source for invariants, scope, and damage
  recipients.

## Inherited Design Tempo

**Autonomy completeness > measurement precision.** Tasks that
propose "let's also add precise telemetry / before-after baseline
/ regression-locking" must be rejected by the implementer; those
belong in future iterations. Ship the loop with proxy signals
first.
