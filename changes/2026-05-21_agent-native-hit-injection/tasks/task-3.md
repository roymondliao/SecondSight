# Task 3: Hook script rewrite + config.toml toggle wiring

## Context

Read: `../overview.md` and `../2-plan.md`.

Task 2 produced the Python helper `secondsight.feedback.hit_injection`
with `render_wrapper(prompt)`. This task wires it into the shell
hook (`scripts/hooks/user-prompt.sh`) AND adds the config toggle
(`[feedback].hit_injection_enabled`, default `true`) that gates
the whole pathway.

Critically, this task must NOT yet delete the legacy curl-to-injection-endpoint
block in the shell hook — that is task-4 (atomic destructive
change). For this task, the shell hook gets an EARLY EXIT block at
the top: if `hit_injection_enabled` is false, exit 0 with no
stdout; if true, invoke the Python helper and emit the wrapped
JSON. The old curl block remains in the file but is unreachable
under normal operation because the new path returns first.

(The old block is preserved temporarily so that task-4's deletion
is one atomic visible diff; otherwise the two diffs interleave.)

## Files

- Modify: `src/secondsight/config/schema.py` — add `hit_injection_enabled: bool = True` to whatever existing FeedbackConfig pydantic model exists (or create one if not present).
- Modify: `src/secondsight/config/loader.py` — extend the loader chain to resolve `[feedback].hit_injection_enabled` with the existing three-source resolution pattern.
- Modify: `src/secondsight/config/template.py` — add the new key to the TOML template emitter so `secondsight init` produces a config with this key documented.
- Modify: `scripts/hooks/user-prompt.sh` — add the early-exit block at the top of `_ss_inject_prompt_guidance`; invoke the Python helper if enabled.
- Modify: `tests/scripts/test_user_prompt_hook_injection.py` — replace the mock-server fixtures with fixtures that test the new Python-helper-driven path (new wrapper rendering, disabled-state behavior).

## Death Test Requirements

**DT-4: Config key missing → silent default to bool True.**
Test name: `test_dt_config_resolves_missing_hit_injection_enabled_to_true_bool`
Given: a `config.toml` without `[feedback].hit_injection_enabled`
When:  loader resolves the value
Then:  value is exactly `True` (Python bool, not truthy string).
       `assert value is True` (identity check).

**DT-5: Config disabled → hook produces no stdout.**
Test name: `test_dt_hook_emits_no_stdout_when_disabled`
Given: config with `hit_injection_enabled = false`; valid stdin payload
When:  `scripts/hooks/user-prompt.sh` is executed with the stdin
Then:  exit code = 0; stdout is empty; no entry in `~/.secondsight/logs/curl-errors.log`
       claiming an error.

**DT-6: Enabled + valid prompt → hook emits well-formed JSON containing wrapper text.**
Test name: `test_dt_hook_emits_wrapper_json_when_enabled`
Given: config with `hit_injection_enabled = true`; stdin payload containing
       a representative prompt (e.g., `"fix the auth bug"`)
When:  `scripts/hooks/user-prompt.sh` is executed with the stdin
Then:  stdout parses as JSON; `additionalContext` is non-empty;
       `additionalContext` contains the original prompt text.

**DT-7: Python interpreter unavailable → hook exits 0 with no stdout.**
Test name: `test_dt_hook_fails_open_when_python_missing`
Given: PATH stripped of `python3` / `python`; config enabled; valid stdin
When:  `scripts/hooks/user-prompt.sh` is executed
Then:  exit code = 0; stdout empty; error appended to
       `~/.secondsight/logs/curl-errors.log` (or equivalent log
       path) naming the missing interpreter.

## Implementation Steps

- [ ] Step 1: Write DT-4 through DT-7.
- [ ] Step 2: Run tests — verify they fail.
- [ ] Step 3: Extend `src/secondsight/config/schema.py` and `loader.py` to support `[feedback].hit_injection_enabled`.
- [ ] Step 4: Extend `src/secondsight/config/template.py` to emit the new key in default config output.
- [ ] Step 5: Modify `scripts/hooks/user-prompt.sh`:
  - At the top of `_ss_inject_prompt_guidance`, read config (via existing helpers in `_lib.sh` if available; otherwise read `[feedback].hit_injection_enabled` from `~/.secondsight/config.toml` directly).
  - If disabled → `return 0` from the function (no stdout).
  - If enabled → invoke Python helper:
    `python3 -c "import sys, json; from secondsight.feedback.hit_injection import render_wrapper; ..."`
    or equivalent; emit `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": rendered}}`.
  - If Python invocation fails → log to curl-errors.log; return 0 (fail-open).
- [ ] Step 6: Rewrite `tests/scripts/test_user_prompt_hook_injection.py`:
  - Remove the mock injection-endpoint server fixtures.
  - Add fixtures that run the hook script as subprocess with various stdin payloads.
  - Layer 1 death test (`test_dt_user_prompt_injection_completes_within_budget_for_1500ms_endpoint`) becomes obsolete — it depends on the endpoint that task-4 will delete; remove this test as part of this task to keep test file coherent.
- [ ] Step 7: Run all tests including DT-4..DT-7.
- [ ] Step 8: Run `rtk proxy pytest tests/scripts/test_user_prompt_hook_injection.py -q`.
- [ ] Step 9: Write scar report.
- [ ] Step 10: Commit.

## Expected Scar Report Items

- Potential shortcut: stubbing `render_wrapper` in the shell test instead of actually invoking the Python helper. Don't — DT-6 must run the real helper to validate the end-to-end JSON shape.
- Potential shortcut: forgetting to handle the case where the legacy curl block in the hook would still fire if the early-exit logic has a bug. Verify in DT-6 that the wrapper text is present and the legacy categorical text is NOT.
- Assumption to verify: that `~/.secondsight/config.toml` is the canonical config path read by the hook. Check `_lib.sh` or existing hook scripts; if they use an env-overridable `SECONDSIGHT_HOME`, follow the same convention.
- Assumption to verify: that the shell hook can spawn `python3` without `uv run` or virtualenv activation in the user's normal environment. If SecondSight is normally invoked via `uv`, the hook may need to detect and use `uv run python3 -c ...`.

## Acceptance Criteria

- Covers: "config toggle missing from config.toml; silent default to wrong value" (death path)
- Covers: "config.toml hit_injection_enabled = false" (degradation)
- Covers: "Python interpreter unavailable on host" (degradation)
- Covers: "enabled, simple prompt, all components present" (happy path)
- Covers: "config toggle preserved with no death condition" (meta invariant)
