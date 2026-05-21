# Task 2: Hit injection wrapper template + Python helper

## Context

Read: `../overview.md` and `../2-plan.md`.

Hit injection is being redesigned from a sidecar classifier to
meta-injection (B-META): the main agent self-evaluates whether
the user's prompt is executable. This task creates the wrapper
template that wraps the user's prompt with an executability
self-check meta-instruction, plus the Python helper that the
shell hook will invoke to render the wrapper.

The wrapper text is the only artifact the main agent will see;
its tone must trust the agent and the user, not lecture either.
The template is rendered via the existing `secondsight.prompts._loader.render`
function. This task does NOT integrate with the shell hook
(that is task-3) and does NOT delete the old sidecar (that is task-4).

## Files

- Create: `src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2`
- Create: `src/secondsight/feedback/hit_injection.py`
- Test:   `tests/feedback/test_hit_injection.py` (new)

The wrapper template's draft starting point is in `../2-plan.md`
under Component #1 "Wrapper template draft". The implementer
should treat it as a starting point and refine for clarity, but
must preserve:
- single template (NOT split into missing_X variants)
- "trust user intent" framing (no enumerated vague categories)
- explicit instruction that the agent should report back in
  natural language what it needs (not invoke a skill, not
  produce structured JSON)
- bypass conditions: empty/whitespace-only prompt passes through
  without wrap

## Death Test Requirements

**DT-1: Quotes / backslashes / newlines in prompt do not break JSON output.**
Test name: `test_dt_wrapper_handles_json_unsafe_characters`
Given: prompt = `'fix the "auth bug\\path" with multi\nline issue'`
When:  `render_wrapper(prompt)` is called and result is wrapped in
       `json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": rendered}})`
Then:  the JSON parses via `json.loads` AND the recovered
       `additionalContext` contains the literal original prompt
       characters.

**DT-2: Empty / whitespace-only prompt → no wrap.**
Test name: `test_dt_wrapper_passes_through_empty_prompt`
Given: prompt = `""` and prompt = `"   \n\t  "`
When:  `render_wrapper(prompt)` is called
Then:  return value is the empty string (no meta-instruction
       surface; nothing to wrap). The hook contract for "no
       wrap" is "additionalContext is empty string", which the
       shell hook in task-3 translates to "no stdout".

**DT-3: Wrapper template file missing → exception raised cleanly.**
Test name: `test_dt_wrapper_raises_on_missing_template`
Given: template file at `src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2` is temporarily removed via monkeypatch
When:  `render_wrapper(prompt)` is called
Then:  a clear exception (e.g., `jinja2.exceptions.TemplateNotFound`
       or wrapped equivalent) is raised, NOT swallowed silently.
       (The shell hook in task-3 is the one responsible for
       fail-open semantics; the Python helper itself must not
       silently degrade.)

## Implementation Steps

- [ ] Step 1: Write DT-1, DT-2, DT-3 (and any happy-path unit tests).
- [ ] Step 2: Run tests — verify they fail with `ModuleNotFoundError` for `secondsight.feedback.hit_injection`.
- [ ] Step 3: Draft `hit_injection_wrapper.jinja2` (start from `../2-plan.md` Component #1 draft; refine wording).
- [ ] Step 4: Implement `src/secondsight/feedback/hit_injection.py` with one public function `render_wrapper(prompt: str) -> str` that:
  - Returns empty string for empty/whitespace-only prompts.
  - Otherwise calls `secondsight.prompts._loader.render("feedback/hit_injection_wrapper", context={"prompt": prompt})`.
  - Raises (does not catch) any rendering exception.
- [ ] Step 5: Run all tests — verify they pass.
- [ ] Step 6: Run `rtk proxy pytest tests/feedback/test_hit_injection.py -q`.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- Potential shortcut: skipping DT-2 because "empty prompts probably don't happen". They do — slash commands, accidental enter. Don't skip.
- Potential shortcut: copying the wrapper text verbatim from `../2-plan.md` without refining. The draft is a starting point; iterate it.
- Assumption to verify: that `prompts._loader.render` accepts dotted template names. Check existing usage in `prompt_evaluator.py:55-56` for the call style — but note that file will be deleted in task-4, so don't import from it.
- Assumption to verify: that empty-string `additionalContext` is treated by Claude Code as "no injection" — if Claude Code logs a warning or rejects it, the contract may need to be "missing key" instead of "empty string"; verify by reading Claude Code hook contract docs.

## Acceptance Criteria

- Covers: "wrapper template substitution leaks unescaped characters into JSON" (death path)
- Covers: "wrapper template file missing" (degradation path) — Python helper's portion: raises cleanly.
- Covers: "enabled, simple prompt, all components present" (happy path) — partial; full coverage with task-3 hook integration.
