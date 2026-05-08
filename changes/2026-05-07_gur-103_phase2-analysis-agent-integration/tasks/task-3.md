# Task 3 (P2-14): `sdk/model_selection.py` — pure select_model()

## Context

Read: `overview.md`, `2-plan.md` §2 (D7), `2-pre-thinking.md` §E.

Pure function returning `(primary_spec, fallback_specs)` from
config + observation records. No side effects, no logging, no
network. Default agent is `claude_code`; `auto` inference is
opt-in.

## Files

- Create: `src/secondsight/sdk/model_selection.py`
- Modify: `src/secondsight/analysis/config.py` (from task-1) —
  add `default_agent`, `[analysis.models.<agent_type>]`,
  `[analysis.models.fallback]` schema if not already present
- Test: `tests/sdk/test_model_selection.py`

## Death Test Requirements

- **DT-3.1 missing config raises ModelSelectionError with config-diff
  suggestion.** Configure project_config without override AND
  global config with `analysis.models.codex = ""`. Assert
  `ModelSelectionError` raised; assert message contains
  `[analysis.models.codex]` and a suggested config snippet
  (e.g., `'set analysis.models.codex = "<model-name>"'`).
- **DT-3.2 'auto' with no events → falls back to claude_code.**
  `default_agent = "auto"`; `events_repo` returns no sessions
  for the project. Assert primary's provider == "anthropic";
  primary's name == "claude-haiku-4-5-20251001" (the SD §5.7.1
  default).
- **DT-3.3 project override beats global.** Project config sets
  `analysis.model = "claude-sonnet-4-6"`; global sets
  `analysis.models.claude_code = "claude-haiku-4-5"`. Assert
  primary == ModelSpec("claude-sonnet-4-6", ...) (project wins).

## Implementation Steps

- [ ] Step 1: Write death tests (3 above).
- [ ] Step 2: Run — verify fail.
- [ ] Step 3: Write happy-path tests:
      - HP-1.3 project override returns expected ModelSpec
      - HP-3.4 'auto' + recent claude_code session →
        claude-haiku-4-5
      - HP-extra: explicit non-auto `default_agent = "codex"` +
        configured `analysis.models.codex = "gpt-5-codex"` →
        primary == that spec
- [ ] Step 4: Run — verify fail.
- [ ] Step 5: Implement:
      - `ModelSpec` dataclass (or import from sdk/router.py if
        defined there — single source of truth).
      - `_ADAPTER_DEFAULTS: dict[str, ModelSpec]`:
        - `"claude_code"` → `ModelSpec("claude-haiku-4-5-20251001",
          "anthropic")`
        - `"codex"` → raises `ModelSelectionError` (SD §5.7.1
          says "TBD Phase 0")
        - `"opencode"` → raises `ModelSelectionError` ("opencode
          requires explicit analysis.model")
      - `select_model(project_id, project_config, global_config,
        events_repo) -> tuple[ModelSpec, list[ModelSpec]]`:
        1. If `project_config.analysis.model` is non-empty →
           primary = parse(that), fallbacks =
           global_config.analysis.models.fallback.fallback_models.
        2. Else: agent_type =
           global_config.analysis.default_agent.
           If `agent_type == "auto"`:
             agent_type = events_repo.
             get_latest_session_agent_type(project_id)
             or "claude_code".
        3. Look up `global_config.analysis.models.<agent_type>`;
           if non-empty and not "auto", primary = parse(that).
           Else, primary = `_ADAPTER_DEFAULTS[agent_type]` (may
           raise ModelSelectionError).
        4. fallbacks =
           global_config.analysis.models.fallback.fallback_models.
        5. Return `(primary, fallbacks)`.
      - `ModelSelectionError(...)` always includes a `suggested_config`
        attribute with the TOML snippet that would resolve the
        issue. Tests assert this verbatim.
- [ ] Step 6: Run — verify pass.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- `_ADAPTER_DEFAULTS` mirrors SD §5.7.1; if that table evolves
  (e.g., codex gets a default after Phase 0), this constant
  must be updated. Co-locate a docstring comment naming the
  SD section.
- `events_repo.get_latest_session_agent_type(project_id)` may not
  exist yet on the events repo — verify and add if needed (1
  query, no schema change).
- The function signature is sync but `events_repo.
  get_latest_session_agent_type` may be async. If so, this
  function becomes async; document the implication for callers
  (router and trigger). If sync repo path is preferred, use
  `asyncio.run` only at the top-level call site, not inside
  `select_model` (which would make it impure).

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DT-3.1, DT-3.2, DT-3.3
- HP-1.3, HP-3.4
