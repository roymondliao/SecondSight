# Overview: directive-injection-runtime

## Goal
Replace the current plain-text directive injection path with a shared server-side injection runtime that returns agent-ready hook payloads for SessionStart conventions and UserPromptSubmit hit guidance.

## Architecture
Injection becomes a dedicated namespace separate from ingest. The server owns selection, evaluation, templates, and fail-open policy; adapters own the final hook payload shape; shell scripts only call the endpoint, print the raw response body, and separately post observation ingest.

## Tech Stack
FastAPI, Pydantic, existing adapter registry, existing config loader/schema, existing CLI/SDK execution mode config, shell hook scripts, pytest.

## Key Decisions
- Separate injection from ingest routes: injection has synchronous raw-payload response semantics; ingest remains async observation transport.
- Respect `config.general.mode` for hit evaluation: CLI mode reuses `SECONDSIGHT_DISABLE_HOOKS=1` recursion guard; SDK mode stays SDK.
- Convention and hit guidance have different lifetimes: Codex SessionStart stays `systemMessage`; UserPromptSubmit stays event-scoped.
- SecondSight owns ambiguity classification contract and fixed guidance templates; the coding agent does not self-decide intervention.

## Death Cases Summary
1. Server selects guidance but adapter renders the wrong payload envelope, so transcript-visible injection never happens.
2. CLI hit evaluation launches a hook-enabled subprocess and recursively emits more hook events.
3. Hit evaluation timeout or malformed output blocks the user's prompt instead of failing open.

## File Map
- `src/secondsight/api/injection.py` — dedicated injection endpoints for SessionStart and UserPromptSubmit.
- `src/secondsight/adapters/base.py` — render seam for final hook payloads.
- `src/secondsight/adapters/claude_code.py` — Claude SessionStart/UserPromptSubmit payload rendering.
- `src/secondsight/adapters/codex.py` — Codex SessionStart/UserPromptSubmit payload rendering with distinct semantics.
- `src/secondsight/feedback/convention.py` — convention selection using resolved feedback budget.
- `src/secondsight/feedback/prompt_guidance.py` — bypass registry and fixed guidance template mapping.
- `src/secondsight/feedback/prompt_evaluator.py` — SecondSight-owned ambiguity evaluator runtime and schema.
- `src/secondsight/config/schema.py` — resolved feedback config section.
- `src/secondsight/config/loader.py` — feedback config loading.
- `scripts/hooks/session-start.sh` — raw payload passthrough for SessionStart injection.
- `scripts/hooks/user-prompt.sh` — sync hit-guidance fetch + async ingest.
- `tests/api/test_injection_session_start.py` — SessionStart injection contract tests.
- `tests/api/test_injection_user_prompt.py` — UserPromptSubmit injection contract tests.
- `tests/feedback/test_prompt_evaluator.py` — hit evaluator and fail-open tests.
