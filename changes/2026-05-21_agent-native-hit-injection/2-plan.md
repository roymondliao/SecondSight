# Plan: agent-native-hit-injection

## Step 1.5 Pre-thinking Resolution

```yaml
gaps_resolved_at_human_gate:
  gap_1_wrapper_template:
    decision: new single template, NOT reuse existing guidance/*.jinja2
    location: src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2
    rationale: existing guidance/ enumerates vague categories — anti-rules residue

  gap_2_wrapper_lang:
    decision: Python helper invoked from shell hook
    rationale: shell stays event-handler only; Python wrapper is testable

  gap_3_double_check_arch:
    decision: reuse existing BehaviorFlag.confidence three-value field
    rationale: do not build new LLM pass; confidence already serves the role

  gap_4_layer_3_red_test_fate:
    decision: delete with prompt_evaluator.py atomic
    rationale: parser removed → test loses subject → no need to make green

  gap_5_migration_call_sites:
    decision: planning-time grep audit completed (5 files identified)
    files:
      - src/secondsight/api/injection.py (surgical removal of /hook/injection/user-prompt/{agent})
      - tests/feedback/test_prompt_evaluator.py (full delete)
      - tests/scripts/test_user_prompt_hook_injection.py (rewrite for new wrapper path)
      - tests/api/test_injection_user_prompt.py (full delete)
      - tests/api/test_injection_session_start.py (audit-only; SessionStart side independent)

  gap_6_double_check_purpose:
    decision: (ii) — operationalized via existing BehaviorFlag.confidence
    artifact: no new code; brief documentation comment in aggregator
    rationale: existing low-confidence ratio = contamination tripwire

  gap_7_wrapper_artifact_definition:
    decision: subsumed by gap_6 resolution; no separate definition needed

  uncertainties_resolved:
    11_atomic_vs_staged: atomic — destructive change ships in one task
    verify_only_regression_tests: NOT required (operator decision)
    config_disabled_behavior: hook exits 0 with no stdout (no additionalContext)
    config_key_name: "[feedback].hit_injection_enabled" (boolean, default true)

undocumented_planning_assumptions:
  - "Reference project (prompt-improver) wrapper format is roughly suitable as starting point; SecondSight wrapper diverges on (a) report-back-via-natural-language tone (not 'use skill'), (b) no enumerated vague categories (single self-evaluation instruction)"
  - "Claude Code hook contract for UserPromptSubmit (stdin JSON, stdout JSON with hookSpecificOutput) is stable; if Anthropic changes it, this plan needs revision"
  - "config.toml [feedback] section already exists or can be created without breaking existing config schema validation"
```

## Step 2: Technical Specification

### Component #1 — Hit Injection Wrapper

**Interface (Python helper)**

```python
# src/secondsight/feedback/hit_injection.py (new module)

def render_wrapper(user_prompt: str) -> str:
    """Render the meta-injection wrapper around the user's prompt.

    Returns text suitable for hookSpecificOutput.additionalContext.
    """
```

**Interface (shell hook)**

```
stdin:  Claude Code UserPromptSubmit JSON payload
stdout (enabled + valid):   {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<wrapped text>"}}
stdout (disabled):          empty (exit 0)
stdout (jq/python missing): empty (exit 0, error logged to curl-errors.log)
```

**Output states** (per skill contract — three, not two):

| State | Condition | Behavior |
|---|---|---|
| `success` | Config enabled + Python + jq present + JSON parses + wrapper renders | stdout = wrapped JSON, exit 0 |
| `failure` | (None — hook design is fail-open) | n/a |
| `unknown` | Python crashes / template missing / jq fails | stdout = empty, exit 0, error appended to `~/.secondsight/logs/curl-errors.log` |

`unknown` treated as fail-open per existing hook contract. No retry, no user-visible error.

**Wrapper template draft** (to be reviewed at task-2 execution time):

```jinja2
{# src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2 #}
EXECUTABILITY SELF-CHECK (SecondSight)

User's request: "{{ prompt }}"

Before acting, evaluate from the executor's perspective: do you have
enough information from this request and the conversation context to
execute the task without making over-broad assumptions or doing more
than the user asked?

If yes: proceed immediately with the request.

If no: tell the user — in natural language, in your own words — what
specific information you need to execute correctly. Do not invent
target files, scope, or success criteria not present in the request
or established earlier in this conversation.

Trust the user's intent. Intervene only when acting immediately is
likely to do the wrong thing.
```

**Death cases**

1. **Wrapper produces JSON that Claude Code rejects as malformed** (silent rot — hook fails open, main agent never sees the wrapper, system appears to work but no executability check happens). **Detect**: contract test that parses stdout as JSON matching the expected schema.

2. **Template substitution leaks shell-special characters into JSON** (e.g., prompt contains `"`, `\n`). System appears to wrap, actually produces invalid JSON. **Detect**: death test with prompts containing quotes, newlines, backslashes, unicode.

3. **Empty/whitespace-only prompt** (Claude Code allows it; wrapper produces meaningless wrap). **Detect**: death test for empty prompt → wrapper should pass through unchanged (no wrap).

### Component #9 — Config Toggle

**Interface**

```toml
# config.toml addition

[feedback]
hit_injection_enabled = true  # bool, default true
```

**Loader contribution**: extend existing config loader in `src/secondsight/config/loader.py` to resolve this key with the standard three-source pattern (env > toml > default).

**Output states**

| State | Behavior |
|---|---|
| `success` (enabled = true, default) | wrapper applied as normal |
| `success` (enabled = false) | hook exits 0 with no stdout; no additionalContext injected |
| `unknown` (config corrupt / key missing) | default true; wrapper applied |

**Death cases**

4. **Config key missing → silent default**: this is intentional, but must be tested so future config schema validator doesn't reject "missing key" as error. **Detect**: test config without this key → resolves to true.

5. **Config truthiness ambiguity**: `"false"` (string) vs `false` (bool) — TOML parses both but Python may differ. **Detect**: death test with stringy false → must still parse as boolean false.

### Component #5 — LLM Double-Check (reduced)

**No new code.** Per gap_6 resolution, the existing `BehaviorFlag.confidence` (Literal["high","medium","low"], `analysis/schemas.py:100`) serves the double-check role.

**Documentation**: add a docstring or inline comment near `BehaviorFlag.confidence` definition explaining its dual role: (a) LLM's self-assessment of analysis quality, (b) tripwire for distribution-shift contamination (per invariant #4 of `1-kickoff.md`).

**Operational instrument** (production-only, no code in this change): operators monitor low-confidence ratio over time; sustained spike = potential contamination signal.

### Component #11 — Atomic Destructive Change

**Files to delete**

```
src/secondsight/feedback/prompt_evaluator.py                 (full delete, 323 lines)
src/secondsight/prompts/feedback/classifier.jinja2           (full delete, 12 lines)
src/secondsight/prompts/feedback/guidance/                   (full directory delete)
tests/feedback/test_prompt_evaluator.py                      (full delete; Layer 3 RED test removed)
tests/api/test_injection_user_prompt.py                      (full delete)
```

**Files to modify**

```
src/secondsight/api/injection.py
  Remove: /hook/injection/user-prompt/{agent} endpoint
  Keep:   /hook/injection/session-start/{agent} endpoint (SessionStart convention path is independent)

tests/scripts/test_user_prompt_hook_injection.py
  Rewrite: replace mock-server fixtures targeting injection endpoint with
           fixtures testing the new Python wrapper helper (rendered output
           shape, edge cases). Layer 1 death test
           (test_dt_user_prompt_injection_completes_within_budget_for_1500ms_endpoint)
           becomes obsolete (no endpoint to time) — delete with the rest.

tests/api/test_injection_session_start.py
  Audit only: verify no implicit dependency on prompt_evaluator imports;
  no functional change expected.

scripts/hooks/user-prompt.sh
  Rewrite: replace curl-to-injection-endpoint block with Python helper invocation
```

**Death cases**

6. **Hook still references deleted endpoint after rewrite** (silent rot — hook exits 0 because of fail-open contract, but no wrapper is actually applied; main agent gets no meta-injection). **Detect**: integration test that runs the hook script with sample stdin and verifies `additionalContext` is populated.

7. **api/injection.py route registration leaves dangling reference** (silent rot — module imports succeed but a stale router has the path). **Detect**: HTTP test that issues POST to deleted endpoint expects 404.

8. **Test deletion misses some test that imports prompt_evaluator** (CI passes because pytest skips missing imports → silent gap in coverage). **Detect**: post-deletion `grep -r "prompt_evaluator" src/ tests/` must return empty.

---

## Step 2.5 Acceptance Criteria

See `acceptance.yaml`. Order is death-first:

1. Death paths (wrapper produces malformed JSON; toggle silently defaults; deletion leaves dangling reference)
2. Degradation paths (jq/Python missing on system; config corrupt)
3. Happy paths (enabled + valid prompt → wrapper applied; disabled → no injection)

---

## Step 3: Task Decomposition

5 self-contained tasks. Each described in `tasks/task-N.md`:

```
task-1: Migration audit + spec lock (no implementation, frozen reference)
task-2: New hit injection wrapper template + Python helper (with death tests)
task-3: Hook script rewrite + config.toml toggle wiring (with death tests)
task-4: Atomic destructive change — delete sidecar + endpoint + obsolete tests
task-5: BehaviorFlag.confidence dual-role documentation
```

**Execution dependency**:

```
task-1 ──┐
         ├──> task-2 ──> task-3 ──> task-4 ──> task-5
task-1 ──┘
```

task-1 must finish (audit known) before task-2/task-3 can write new code (no risk of conflict).
task-4 must come after task-2 + task-3 are complete (new wrapper path must be working before old is removed).
task-5 is documentation; can be last or parallel to task-4.

## File Map (consolidated)

```
NEW:
  src/secondsight/feedback/hit_injection.py
  src/secondsight/prompts/feedback/hit_injection_wrapper.jinja2

MODIFIED:
  src/secondsight/api/injection.py
  src/secondsight/config/loader.py
  src/secondsight/config/schema.py            (add FeedbackConfig.hit_injection_enabled)
  src/secondsight/config/template.py          (add toml template entry)
  scripts/hooks/user-prompt.sh
  tests/scripts/test_user_prompt_hook_injection.py
  src/secondsight/analysis/schemas.py         (docstring update for BehaviorFlag.confidence)

DELETED:
  src/secondsight/feedback/prompt_evaluator.py
  src/secondsight/prompts/feedback/classifier.jinja2
  src/secondsight/prompts/feedback/guidance/   (entire directory)
  tests/feedback/test_prompt_evaluator.py
  tests/api/test_injection_user_prompt.py

AUDIT-ONLY (no expected change):
  tests/api/test_injection_session_start.py
```

## Risks Surfaced During Planning

- **Wrapper template effectiveness is not unit-testable.** We can test the JSON shape, but whether the wrapper actually makes the main agent perform executability self-check is not deterministic — it depends on LLM behavior. This is a known limitation; the actual effectiveness will be observed via BehaviorFlagType rate movement post-ship (North Star).
- **Atomic delete creates one-shot risk.** Cannot partial-ship. If integration tests fail after deletion, rollback is a single revert. Planning accepts this per the "no hedging" Q1 framing.
- **config.toml schema migration**: existing installations on disk may not have `[feedback].hit_injection_enabled`. Default true ensures backward compat, but if SecondSight has strict-mode config validation, that needs a known-default carve-out.
