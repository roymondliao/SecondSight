# Overview: analysis-mode-toggle

## Goal

Restore SecondSight analysis layer to the SD-promised dual-mode dispatch (SDK + CLI subprocess) with `[general].mode` config toggle, fix the production `ANTHROPIC_API_KEY` silent failure, and structurally prevent recurrence of the dishonest-naming rot that obscured the design-implementation gap.

## Architecture

Mode-awareness is centralized in `ProjectAnalysisRuntime.dispatch()` — all callers (session_end, sweeper, manual `secondsight analyze`) remain mode-agnostic and go through this single entry. Two dispatchers (`SDKAnalysisDispatcher`, `CLIAnalysisDispatcher`) share a single pydantic output contract (`AnalysisOutput`); the caller cannot tell which mode produced the result. Config is single-layer global (`~/.secondsight/config.toml`); per-project override is frozen for this effort and deferred to `TODO.md`.

## Tech Stack

- Python 3.12+, `uv` for dependencies (per `AGENTS.md`)
- `pydantic` for `AnalysisOutput` contract and config dataclasses
- `pydantic-ai` for SDK-mode agent loop (existing)
- `jinja2` (`StrictUndefined`) for prompt templates
- `asyncio.create_subprocess_exec` for CLI dispatch
- `tomllib` for config parsing (existing)
- `loguru` for logging (per `AGENTS.md`)
- `pytest` for tests

## Key Decisions

- **Decision A (research)**: ANTHROPIC_API_KEY bug fix bundled with toggle effort; pre-check is mode-conditional, dependency on `[general].mode` makes them inseparable
- **Decision B → B4 (research)**: `[analysis]` split into nested `[analysis.cli]` / `[analysis.sdk]` subsections; loader reads only the subsection matching `mode`
- **Decision C (research)**: CLI mode supports `claude_code` + `codex`; `opencode` schema slot preserved but dispatcher rejects with actionable error
- **Decision D (research)**: `[general].mode` defaults to `"cli"`; aligns with SD + Anthropic Claude Agent SDK billing change
- **Decision E1 (research)**: `${VAR}` is the ONLY env-injection mechanism; no implicit env fallback
- **Decision E3 (research)**: SDK fallback collapsed from list to single string
- **Decision E4 (research)**: retention TTL defaults shortened 90/365 → 30/60
- **Decision E5/E7 (research)**: `[analysis.cli.models]` empty values = "let coding agent use its own default model"; SecondSight does not proxy model selection
- **Decision E6 (research)**: per-project config entirely frozen for this effort (see `TODO.md` "Per-Project Config Override: Deferred")
- **Decision E7 (research)**: `default_agent = "auto"` resolves to per-machine `~/.secondsight/state.json` written at `secondsight init` time
- **Planning Decision #1**: prompts refactored to jinja2 (English) under `src/secondsight/prompts/`, ride-along upgraded to must-have for cross-mode template reuse
- **Planning Decision #2**: pydantic schema-feedback retry loop, capped at 2 retries, both modes
- **Planning Decision #3**: pre-check runs at server startup only (not at `secondsight init`); resolved keys cached at config load (no per-dispatch re-resolution)
- **Planning Decision #4**: mode dispatch lives in `ProjectAnalysisRuntime.dispatch()`; sweeper/manual CLI remain mode-agnostic

## Death Cases Summary

Top 3 most dangerous silent failure paths (full set DC1-DC12 in `acceptance.yaml`):

1. **DC2 — CLI subprocess returns valid JSON with wrong schema**: looks like a successful analysis, but `intelligence.db` row is malformed. Detected by `pydantic.AnalysisOutput` strict validation + bounded retry (≤2).
2. **DC7 — User has `$ANTHROPIC_API_KEY` in env, config has empty `ANTHROPIC_API_KEY = ""` (no `${VAR}`)**: SD §8.5.3 mental model says env fallback should apply, but E1 deviation removes that. Detected by pre-check at server startup with actionable error message naming the exact remediation (`"${ANTHROPIC_API_KEY}"`).
3. **DC9 — Jinja template renders with missing context variable**: silent empty prompt → coding agent does nothing meaningful → empty `behavior_flags`. Detected by `StrictUndefined` jinja env raising at render time, before subprocess is even spawned.

## File Map

### New files

- `src/secondsight/state.py` — `SecondSightState` dataclass + `state.json` read/write
- `src/secondsight/config/precheck.py` — mode-conditional startup pre-check
- `src/secondsight/analysis/output.py` — `AnalysisOutput` pydantic model (shared CLI/SDK contract)
- `src/secondsight/analysis/cli_dispatcher.py` — `CLIAnalysisDispatcher` + per-agent adapters
- `src/secondsight/analysis/sdk_dispatcher.py` — thin wrapper around existing PydanticAI path
- `src/secondsight/prompts/_loader.py` — jinja2 template loader with `StrictUndefined`
- `src/secondsight/prompts/analysis/behavior.jinja2` — behavior analysis prompt
- `src/secondsight/prompts/analysis/summary.jinja2` — session summary prompt
- `src/secondsight/prompts/analysis/aggregate.jinja2` — cross-session aggregation prompt
- `config.example.toml` (repo root) — locked config template (mirrors planning artifact)

### Modified files

- `src/secondsight/config/schema.py` — add `GeneralConfig`, `ProvidersConfig`, nested `AnalysisCLIConfig` / `AnalysisSDKConfig`
- `src/secondsight/config/loader.py` — parse new sections; `${VAR}` interpolation already exists; warn-and-ignore legacy flat `[analysis] default_agent`; resolve provider keys once at load
- `src/secondsight/analysis/runtime.py` — `ProjectAnalysisRuntime.dispatch()` branches by mode
- `src/secondsight/analysis/prompts/{behavior,summary,aggregate}.py` — replace string constants with jinja loader calls
- `src/secondsight/sdk/router.py` — `LLMRouter.__init__` accepts `resolved_keys`; explicit `AnthropicProvider(api_key=...)`
- `src/secondsight/cli/init.py` — write `~/.secondsight/state.json` on init; prompt-on-overwrite (DC11)
- `src/secondsight/cli/serve.py` — call `precheck()` at startup, exit non-zero on failure
- `TODO.md` — entry already added at research stage for "Per-Project Config Override: Deferred"
