# Task 3: Prompts jinja2 refactor (StrictUndefined, English templates)

## Context

Read: `overview.md`, `2-plan.md` §6.

Current state: `src/secondsight/analysis/prompts/{behavior,summary,aggregate}.py` hold Python string constants (some f-string interpolation, some `.format()`). These strings will be consumed by BOTH the SDK dispatcher (as PydanticAI system prompt) and the CLI dispatcher (as stdin to coding agent CLI). Without a shared template source, the two paths can silently drift apart.

This task moves prompt strings into `src/secondsight/prompts/analysis/*.jinja2` templates with `StrictUndefined` enabled — any undeclared variable raises `jinja2.UndefinedError` at render time. Silent empty rendering is forbidden (DC9).

All templates are written in English (per user direction).

**Scope clarification (per user direction 2026-05-14)**: Task 3 produces **first-draft prompts** — faithful migration of existing Python constants into jinja2 + schema embedding. Task 4 will iterate on these prompts inside its PoC phase (up to 3 prompt variants per agent) and MAY modify `*.jinja2` files in this directory to land on the winning variant. Task 3 does NOT need to anticipate every coding-agent quirk — Task 4 has explicit budget to refine. The bar for Task 3 acceptance is: prompt renders cleanly (no `StrictUndefined` errors), contains all variables present in the original Python constants, and embeds `AnalysisOutput.model_json_schema()` for CLI mode use. Prompt-quality optimization for specific coding agents = Task 4's job.

## Files

- Create: `src/secondsight/prompts/__init__.py`
- Create: `src/secondsight/prompts/_loader.py`
- Create: `src/secondsight/prompts/analysis/__init__.py`
- Create: `src/secondsight/prompts/analysis/behavior.jinja2`
- Create: `src/secondsight/prompts/analysis/summary.jinja2`
- Create: `src/secondsight/prompts/analysis/aggregate.jinja2`
- Modify: `src/secondsight/analysis/prompts/behavior.py` — replace string constant with `_loader.render("analysis/behavior", context=...)` call
- Modify: `src/secondsight/analysis/prompts/summary.py` — same pattern
- Modify: `src/secondsight/analysis/prompts/aggregate.py` — same pattern
- Test: `tests/prompts/test_loader.py`
- Test: `tests/prompts/test_analysis_templates.py`
- Test: `tests/analysis/prompts/test_render_compat.py` (regression: rendered output equivalent to old constant)

## Death Test Requirements

Before any implementation:

- Test: `_loader.render("analysis/behavior", context={})` when the template references `{{ session_id }}` → raises `jinja2.UndefinedError` containing `session_id` in the message (NOT silent empty render)
- Test: `_loader.render("nonexistent/template")` → raises `TemplateNotFound`
- Test: `_loader.render("analysis/behavior", context={...complete...})` → returned string is non-empty AND contains no `{{` or `}}` literals (renders cleanly)
- Test: the JSON schema for `AnalysisOutput` is rendered into `behavior.jinja2` (so CLI prompts can instruct coding agent on the exact output shape) — verify by string-search the rendered output for `"schema_version": "1.0"` literal
- Test: rendered template for behavior prompt with a fixed input context matches the old string constant byte-for-byte after a known equivalent input (regression — `test_render_compat.py`); if NOT byte-equivalent (jinja whitespace, etc.), diff is captured and approved explicitly in the scar report
- Test: jinja env is configured with `autoescape=False` (analysis prompts are NOT HTML — escape would corrupt JSON/code blocks in prompts)

## Implementation Steps

- [ ] Step 1: Audit existing prompts (`behavior.py`, `summary.py`, `aggregate.py`) — list ALL interpolation points (f-string variables, `.format()` keys, `.replace()` patterns)
- [ ] Step 2: Write `tests/prompts/test_loader.py` death tests
- [ ] Step 3: Run death tests — verify import failure
- [ ] Step 4: Write `test_render_compat.py` regression test using a synthetic context (compares rendered template to existing constant output with same inputs)
- [ ] Step 5: Implement `src/secondsight/prompts/_loader.py`:
  - `Environment(loader=PackageLoader("secondsight", "prompts"), undefined=StrictUndefined, autoescape=False)`
  - `render(template_name: str, context: dict) -> str`
- [ ] Step 6: Migrate `behavior.py` constant to `behavior.jinja2` — use audit list from Step 1 to ensure NO variable is dropped
- [ ] Step 7: Migrate `summary.py` constant to `summary.jinja2`
- [ ] Step 8: Migrate `aggregate.py` constant to `aggregate.jinja2`
- [ ] Step 9: Update `src/secondsight/analysis/prompts/{behavior,summary,aggregate}.py` to call `_loader.render()` — keep public API (function names) backward compatible if used externally
- [ ] Step 10: Embed `AnalysisOutput.model_json_schema()` as a string into `behavior.jinja2` (and `summary.jinja2` if it also returns structured output) — coding agent CLIs need this to produce conformant JSON
- [ ] Step 11: Run all tests — verify pass
- [ ] Step 12: Run `pre-commit run --all-files`
- [ ] Step 13: Write scar report (must include the regression diff if Step 4 test did NOT pass byte-equal — explicitly note what changed and why it's acceptable)
- [ ] Step 14: Commit

## Expected Scar Report Items

- Potential shortcut: writing prompts in 中文 because some context tokens (session messages) may be 中文. **Don't.** User specified English per `level-analysis` answer. Mixed-language prompts are OK if the surrounding instructions are English.
- Potential shortcut: skipping the `extra="forbid"` ↔ `StrictUndefined` symmetry. Both fail loud on hidden drift; one without the other leaves a gap.
- Assumption to verify: existing prompt strings have NO `{` or `}` characters that aren't variable interpolation — if they contain literal JSON example blocks, jinja will parse those as expression markers unless escaped (`{% raw %}...{% endraw %}`).
- Assumption to verify: `pyproject.toml` includes `jinja2` as a dependency. If not, add it.
- Watch for: `PackageLoader` requires the prompts directory to be a Python package (`__init__.py` files at every level). Forget one and you get `TemplateNotFound`.
- Watch for: trailing newlines — jinja templates by default trim or preserve whitespace differently per the env config; pick `trim_blocks=True, lstrip_blocks=True` for consistency and document the choice.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DC9 (jinja missing context variable → UndefinedError, not silent empty render)
- Happy path "subprocess invoked with rendered behavior.jinja2" (CLI mode evidence chain)
