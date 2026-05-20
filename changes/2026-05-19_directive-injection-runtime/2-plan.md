# Plan: directive-injection-runtime

## 1. Architecture

This feature introduces a dedicated **injection runtime** separate from event
ingest:

```text
hook script
  -> POST /hook/injection/<moment>/{agent}
  -> server-side selection/classification
  -> adapter-specific final hook payload render
  -> raw stdout payload returned to hook
  -> hook prints payload unchanged
  -> separate async POST /hook/{agent}/{event_type} for observation ingest
```

There are two moments:

- `session-start` for project-scoped convention injection
- `user-prompt` for prompt-scoped hit guidance

The shell layer stays transport-only. Server-side runtime owns:

- convention candidate fetch
- budget fit
- convention template assembly
- bypass registry
- hit evaluation
- fixed guidance template mapping

The adapter layer owns only the final hook payload shape:

- Claude Code:
  - SessionStart: `hookSpecificOutput.additionalContext`
  - UserPromptSubmit: `hookSpecificOutput.additionalContext`
- Codex:
  - SessionStart: top-level `systemMessage`
  - UserPromptSubmit: event-scoped hook output, not session-scoped `systemMessage`

## 2. Runtime Surfaces

### 2.1 Injection API

Add a dedicated injection router:

- `POST /hook/injection/session-start/{agent}`
- `POST /hook/injection/user-prompt/{agent}`

Request bodies:

- SessionStart:
  - `project_id: str`
- UserPromptSubmit:
  - `project_id: str`
  - `prompt: str`
  - `session_id: str | None`

Response contract:

- success with payload:
  - raw hook stdout payload, already rendered for the target agent
- no-op / bypass / fail-open:
  - `204 No Content`

The old `/hook/session-start` injection route should be removed or replaced so
tests cannot accidentally keep exercising the obsolete `{conventions, count,
budget_*}` JSON contract.

### 2.2 Adapter Rendering Contract

Extend `AgentAdapter` beyond line formatting:

- `inject_convention(convention) -> str`
- `render_session_start_output(text: str) -> str`
- `render_user_prompt_output(text: str) -> str`

Rationale:

- `inject_convention()` remains line/item formatting
- final event/session payload rendering must be explicit and agent-specific
- `inject_hint()` remains reserved; B v1 does not depend on persisted hints

### 2.3 SessionStart Convention Path

Flow:

1. hook derives `project_id`
2. server loads project resources
3. `ConventionSelector` fetches active conventions
4. budget fit uses resolved config budget
5. server wraps selected items in the convention template
6. adapter renders final SessionStart payload
7. hook prints payload unchanged

Budget source:

- add resolved `feedback` config to `SecondSightConfig`
- use `feedback.convention_injection_budget` at runtime

### 2.4 UserPromptSubmit Hit Path

Flow:

1. hook derives `project_id`, extracts raw prompt, forwards optional `session_id`
2. server checks agent-scoped bypass registry
3. bypass match => `204`
4. no bypass => run SecondSight-owned LLM ambiguity evaluator
5. evaluator returns:
   - `decision = pass | intervene`
   - `primary_category = one of four categories | null`
6. `pass` => `204`
7. `intervene` => map category to fixed guidance template
8. adapter renders final UserPromptSubmit payload
9. hook prints payload unchanged

Hit categories in v1:

- `missing_target`
- `multiple_interpretations`
- `missing_scope`
- `missing_success_criteria`

### 2.5 Hit Evaluator Runtime

The hit evaluator must respect configured runtime mode:

- `general.mode = sdk`
  - evaluator uses SDK path
- `general.mode = cli`
  - evaluator uses CLI subprocess path

CLI mode must inherit the existing recursion guard:

- evaluator subprocess env is filtered through the same hook-disable policy
- `SECONDSIGHT_DISABLE_HOOKS=1` must be present

This subprocess is a classifier worker only:

- it must not emit observation events
- it must not receive convention/hit injection
- it must fail open if it times out or returns malformed output

The evaluator prompt is a strict classifier prompt:

- trust user intent by default
- intervene only when genuinely unclear
- if uncertain, choose `pass`
- JSON only
- no rewritten prompt
- no user-facing guidance generation

## 3. I/O and Unknown Output States

### 3.1 SessionStart Injection

Input:

- `agent`
- `project_id`

Outputs:

- `success`:
  - raw hook payload body returned
- `failure`:
  - request invalid (`422`)
  - adapter cannot render (`501`)
- `unknown`:
  - server temporarily unavailable / config read failure / unexpected render error
  - runtime degrades to `204` or empty hook output so agent launch is not blocked

### 3.2 UserPrompt Injection

Input:

- `agent`
- `project_id`
- `prompt`
- optional `session_id`

Outputs:

- `success`:
  - raw hook payload body returned
- `failure`:
  - invalid request (`422`)
- `unknown`:
  - evaluator timeout
  - evaluator malformed JSON
  - CLI subprocess boot failure
  - SDK provider failure

Unknown must degrade to:

- `204`
- hook exits `0`
- async ingest still happens

Treating unknown as hard failure is a defect because it blocks user prompts.

## 4. Death Cases

### DC1 — The Lie of "Injected" Without Visible Context

Trigger:

- server selects conventions or hit guidance, but adapter renders the wrong
  output envelope for the target agent

The lie:

- API logs and DB rows say injection succeeded

The truth:

- the agent transcript/capture sees no usable injected context

Detection:

- adapter contract tests assert exact payload shape
- hook stdout tests assert raw body passthrough
- at least one contract-level test per agent/moment checks the returned JSON
  shape, not plain text presence

### DC2 — Recursive Hook Storm From CLI Hit Evaluation

Trigger:

- CLI-mode hit evaluator launches a coding-agent subprocess without
  `SECONDSIGHT_DISABLE_HOOKS=1`

The lie:

- the evaluator is "just another LLM call"

The truth:

- nested hook sessions emit recursive observation/injection events, corrupting
  traces and adding latency/noise

Detection:

- env-filter unit tests assert hook-disable flag on evaluator subprocess
- death tests assert no hook-side transport is invoked during evaluator runs

### DC3 — Fail-Open Contract Broken on Ambiguity Evaluator Failure

Trigger:

- evaluator timeout or malformed output propagates as hard error to the hook

The lie:

- prompt guidance is optional

The truth:

- the feature blocks user prompts or causes non-zero hook exits

Detection:

- hook script tests assert exit code `0` and empty stdout on server timeout
- API tests assert `204` on evaluator `pass`, bypass, and handled failure paths

### DC4 — Config Budget Documented but Ignored

Trigger:

- SessionStart selection still uses hard-coded 2000 token budget

The lie:

- operators can tune convention injection budget

The truth:

- runtime ignores configured feedback budget and may over-inject or under-inject

Detection:

- config loader/schema tests for `[feedback]`
- session-start API tests with non-default budget values

### DC5 — Codex Session-Level and Event-Level Payload Semantics Mixed

Trigger:

- Codex SessionStart uses event-scoped payload or UserPromptSubmit uses
  session-scoped `systemMessage`

The lie:

- one generic payload contract works for every injection moment

The truth:

- SessionStart and UserPromptSubmit have different semantic lifetimes; mixing
  them causes invisible no-op injection or wrong prompt placement

Detection:

- distinct adapter render tests for `render_session_start_output()` and
  `render_user_prompt_output()`

## 5. File Map

- Create: `src/secondsight/api/injection.py`
- Modify: `src/secondsight/api/server.py`
- Modify: `src/secondsight/adapters/base.py`
- Modify: `src/secondsight/adapters/claude_code.py`
- Modify: `src/secondsight/adapters/codex.py`
- Modify: `src/secondsight/feedback/convention.py`
- Create: `src/secondsight/feedback/prompt_guidance.py`
- Create: `src/secondsight/feedback/prompt_evaluator.py`
- Modify: `src/secondsight/config/schema.py`
- Modify: `src/secondsight/config/loader.py`
- Modify: `src/secondsight/config/template.py`
- Modify: `scripts/hooks/session-start.sh`
- Modify: `scripts/hooks/user-prompt.sh`
- Test: `tests/api/test_injection_session_start.py`
- Test: `tests/api/test_injection_user_prompt.py`
- Modify: `tests/scripts/test_hook_fallback.py`
- Modify: `tests/installer/test_claude_settings.py`
- Modify: `tests/installer/test_codex_hooks.py`
- Modify: `tests/adapters/test_claude_code.py`
- Modify: `tests/adapters/test_codex.py`
- Test: `tests/feedback/test_prompt_guidance.py`
- Test: `tests/feedback/test_prompt_evaluator.py`

## 6. Implementation Strategy

### 6.1 Foundation First

Build the config and adapter seams before changing shell behavior:

- resolved feedback config
- adapter render methods
- new injection router namespace

This prevents the shell migration from encoding transport assumptions that the
server later has to unwrap.

### 6.2 Migrate SessionStart Before UserPrompt

SessionStart is deterministic and local:

- DB read
- budget fit
- template
- render

It validates the transport seam without LLM runtime complexity. UserPrompt hit
guidance should only be added after SessionStart payload rendering is proven.

### 6.3 Reuse Existing Hook-Disable Guard

Do not invent a second recursion mechanism. CLI hit evaluation must reuse the
existing `SECONDSIGHT_DISABLE_HOOKS=1` contract already established in analysis
CLI paths.

### 6.4 Fail Open Everywhere on B

Hit guidance is optional. Any unexpected evaluator failure path degrades to:

- no guidance injected
- hook exit `0`
- observation ingest still posted

## 7. Exit Condition

Planning is complete when implementation can produce:

- SessionStart raw payloads rendered correctly per agent
- UserPromptSubmit raw payloads rendered correctly per agent
- no plain-text-only shell injection assumptions remain
- CLI hit evaluation cannot recurse through hooks
- configured convention budget is honored at runtime
- tests verify output shape and fail-open behavior rather than only string presence
