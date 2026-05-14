# Task 1: Config schema + state.json + loader (foundation)

## Context

Read: `overview.md`, `2-plan.md` §1, §5, §8.

This task is the foundation for mode-aware dispatch. It introduces the `[general]` / `[providers.*]` / nested `[analysis.cli]` / `[analysis.sdk]` config sections, the `~/.secondsight/state.json` for init-time agent persistence, and updates the loader to (a) resolve `${VAR}` interpolation for provider keys once at load, (b) warn-and-ignore the legacy flat `[analysis] default_agent` field for backward compatibility.

This task does NOT touch dispatch logic — runtime.py and dispatcher modules are untouched. Other tasks build on this foundation.

The locked config schema lives in `changes/2026-05-14_analysis-mode-toggle/config.example.toml`. Use it as the source of truth for field names, default values, and structure.

## Files

- Create: `src/secondsight/state.py`
- Modify: `src/secondsight/config/schema.py` — add new dataclasses (`GeneralConfig`, `ProvidersConfig`, `ProviderAnthropicConfig`, `ProviderOpenAIConfig`, `ProviderCustomConfig`, `AnalysisCLIConfig`, `AnalysisCLIModelsConfig`, `AnalysisSDKConfig`, `AnalysisConfig`) and update `SecondSightConfig` aggregate
- Modify: `src/secondsight/config/loader.py` — parse new sections, resolve `${VAR}` for `[providers.*]`, warn-and-ignore legacy `[analysis] default_agent` flat key
- Modify: `src/secondsight/cli/init.py` — write `~/.secondsight/state.json` after stage 2; prompt-on-overwrite if state.json exists with different `init_agent` (skip prompt if `--force`)
- Create: `config.example.toml` (repo root) — copy from `changes/2026-05-14_analysis-mode-toggle/config.example.toml`
- Test: `tests/config/test_schema_v2.py`
- Test: `tests/config/test_loader_v2.py`
- Test: `tests/config/test_state.py`
- Test: `tests/cli/test_init_state.py`

## Death Test Requirements

Before any implementation:

- Test: legacy config `[analysis] default_agent = "claude_code"` (flat) → loader emits WARN containing the substring `legacy [analysis] default_agent` and the resolved config has `general.mode == "cli"` (built-in default). Loader does NOT raise.
- Test: `~/.secondsight/state.json` missing → `SecondSightState.load()` returns `None` (not exception).
- Test: `~/.secondsight/state.json` malformed JSON → raises `SecondSightStateError` with the bad path in the message.
- Test: `state.json.init_agent` value is `"opencode"` → `SecondSightState.load()` succeeds (schema-valid) BUT downstream resolution rejection happens in Task 6 (not here).
- Test: `secondsight init --agent codex` when `state.json` already has `init_agent="claude_code"` → CLI prompts; default answer N → state.json unchanged; explicit `--force` → state.json overwritten with new `init_at` timestamp.
- Test: `[providers.anthropic] ANTHROPIC_API_KEY = "${MY_KEY}"` with `MY_KEY` unset → loader raises `SecondSightConfigError` (interpolation failure, existing loader behavior — assert it propagates correctly).
- Test: empty `[providers.anthropic] ANTHROPIC_API_KEY = ""` → loader resolves it to empty string (NOT to env `$ANTHROPIC_API_KEY`; this is E1).
- Test: `[general] mode = "invalid"` → loader raises `SecondSightConfigError` (only `"cli"` and `"sdk"` accepted).
- Test: writing then loading state.json round-trips all fields (schema_version, init_agent, init_at, secondsight_version).

## Implementation Steps

- [ ] Step 1: Write death tests (above) — they should reference yet-to-exist dataclasses / functions
- [ ] Step 2: Run death tests — verify they fail (ImportError or AssertionError)
- [ ] Step 3: Write happy-path unit tests for each new dataclass (default values, frozen behavior)
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement `src/secondsight/state.py` with `SecondSightState` dataclass, `load()`, `save()`, `SecondSightStateError`
- [ ] Step 6: Add new dataclasses to `src/secondsight/config/schema.py`; update `SecondSightConfig` aggregate to include `general: GeneralConfig`, `providers: ProvidersConfig`, `analysis: AnalysisConfig` (replacing the old flat `analysis: GlobalAnalysisConfig`)
- [ ] Step 7: Update `src/secondsight/config/loader.py` to parse new sections + warn-and-ignore legacy flat field
- [ ] Step 8: Update `src/secondsight/cli/init.py` to write `state.json` (with prompt-on-overwrite + `--force`)
- [ ] Step 9: Copy `config.example.toml` from research artifact to repo root
- [ ] Step 10: Run all tests — verify they pass
- [ ] Step 11: Run `pre-commit run --all-files` (per `AGENTS.md`)
- [ ] Step 12: Write scar report → `changes/2026-05-14_analysis-mode-toggle/scar-reports/task-1.md`
- [ ] Step 13: Commit (`feat(config): add general/providers/nested-analysis schema + state.json (GUR-???)` — substitute correct GUR ticket number)

## Expected Scar Report Items

- Potential shortcut: dropping `GlobalAnalysisConfig` entirely instead of keeping it for warn-and-ignore detection. **Don't.** The legacy flat parse path must remain explicit, not implicit, so that detection logic is testable.
- Potential shortcut: skipping the `--force` flag on `secondsight init` re-run. **Don't.** DC11 requires it (scripted re-init is a real flow).
- Assumption to verify: `tomllib` (3.11+) parses `[providers.custom]` with `base_url` correctly — should be trivial, but TOML quoting is a known footgun.
- Assumption to verify: existing tests under `tests/config/` use `GlobalAnalysisConfig` directly; you'll need to update them to use the new `AnalysisConfig`. Audit imports.
- Assumption to verify: `~/.secondsight/` directory may not exist on first `secondsight init` run — `state.save()` must `mkdir(parents=True, exist_ok=True)`.
- Watch for: empty string semantics for `${VAR}` — confirm loader's existing `_interpolate_vars` returns `""` when env var IS set to empty string (vs unset). Tests must distinguish.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DC11 (init overwrite prompt)
- DC12 (legacy flat config warn-and-ignore)
- DC7 partial (no implicit env fallback — verified at config level here; full pre-check coverage is Task 6)
- "Upgrade — fresh install with no config.toml gets default mode=cli"
- "Upgrade — existing user with legacy flat config gets WARN but server still starts" (partial — full server-start path is Task 6)
