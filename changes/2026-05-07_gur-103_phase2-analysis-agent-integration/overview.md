# Overview: GUR-103 Phase 2 Analysis Agent Integration

## Goal

Bind the existing GUR-102 orchestration pipeline to a real LLM via a
PydanticAI agent, with LiteLLM as escape-hatch provider and three
trigger paths (session-end event, periodic timeout sweeper, manual
CLI) converging on exactly-once dispatch.

## Architecture

Two new layers, no new tables, no schema changes:

1. **Tools layer (`analysis/tools.py`)** — domain-level, framework-
   agnostic. Four methods (`read_traces`, `read_project_file`,
   `query_structured_store`, `read_historical_flags`).
2. **SDK layer (`sdk/`)** — concrete `PydanticAIAnalysisAgent`
   implementing the frozen Protocol from `analysis/agent.py`,
   plus `LLMRouter` with transport-error-only fallback,
   plus pure `select_model()` function, plus `Trigger` with
   per-session asyncio lock for dedup.

CLI: `secondsight analyze [--session ID] [--project P] [--force]`
prefers server-mode (HTTPX), falls back to in-process. Pipeline
subscription: a one-line callback in `observation/pipeline.py`
schedules dispatch on `EventType.SESSION_END` after DB write.

## Tech Stack

- `pydantic-ai` (pin at planning time; minor-version bumps trigger
  regression test pass)
- `litellm` as a *provider option* for non-OpenAI-compatible
  providers, NOT as the router (D6)
- `httpx` (already present) for CLI server-mode talk
- `Typer` (already present) for the CLI subcommand
- SQLAlchemy Core (already present) for any new repo queries
- `asyncio.Lock` + weakref dict for per-session dedup

## Key Decisions

- **D1.** Tools live in `analysis/`, not `sdk/` — domain-agnostic.
- **D2.** Reuse existing `analysis_runs` table for trigger dedup;
  no new state table.
- **D3.** Hook handler unchanged; subscription is post-ingest.
- **D4.** Per-method tool scoping prevents aggregator from reading
  raw project files (impossible at the agent layer).
- **D5.** Router fallback fires ONLY on transport-error allowlist;
  ValidationError bubbles immediately. Cost-leak control.
- **D6.** Direct PydanticAI per-provider; LiteLLM is escape hatch,
  not the router.
- **D7.** `default_agent = "claude_code"` is the default; `auto`
  is opt-in.
- **D8.** `read_project_file` ships available with sandbox +
  default-deny denylist. Optional per-project disable flag.
- **D11.** Fallback chain default: `["gpt-4o-mini",
  "gemini-2.0-flash"]` (SD §5.7.2).

## Death Cases Summary

Top 3 most dangerous silent-failure paths (all from `acceptance.yaml`):

1. **DC-1: Symlink sandbox bypass** — `read_project_file` follows
   a symlink out of the project root, leaks `/etc/passwd` to LLM.
   Closed by `Path.resolve(strict=True)` + `is_relative_to`
   re-check on resolved path.
2. **DC-3: ValidationError masquerading as transport error** —
   router triggers fallback on a wrapped `ValidationError`,
   pays 3× for the same broken prompt. Closed by `__cause__`
   chain unwrapping in router's failure classifier.
3. **DC-4: Trigger race produces duplicate dispatches** —
   event-driven and sweeper both fire within ms; cost doubles.
   Closed by `lock_registry.session_lock(session_id)` with
   non-blocking acquire.

## File Map

- `src/secondsight/analysis/tools.py` — `AnalysisTools`,
  `ProjectFileToolError`, `StructuredQuery`. ~250 LOC.
- `src/secondsight/analysis/config.py` — `AnalysisConfig` Pydantic
  model + TOML loader. ~150 LOC. Modeled on GUR-147's
  `RetentionConfig`.
- `src/secondsight/sdk/__init__.py` — public exports.
- `src/secondsight/sdk/agent.py` — `PydanticAIAnalysisAgent`
  implementing the Protocol with per-method tool scoping. ~250 LOC.
- `src/secondsight/sdk/router.py` — `LLMRouter`, failure
  classifier with `__cause__` chain unwrapping. ~200 LOC.
- `src/secondsight/sdk/model_selection.py` — pure `select_model()`
  + `ModelSpec` + `ModelSelectionError`. ~120 LOC.
- `src/secondsight/sdk/trigger.py` — `Trigger`, `LockRegistry`,
  `Sweeper`. ~250 LOC.
- `src/secondsight/cli/analyze.py` — Typer subcommand. ~150 LOC.
- `src/secondsight/observation/pipeline.py` — modified: 1 callback
  for `EventType.SESSION_END` post-ingest.
- `src/secondsight/cli/app.py` — modified: register `analyze`.
- `src/secondsight/api/server.py` — modified: start sweeper in
  lifespan, cancel on shutdown.
- Tests: `tests/analysis/test_tools.py`, `tests/sdk/test_router.py`,
  `tests/sdk/test_model_selection.py`, `tests/sdk/test_agent.py`,
  `tests/sdk/test_trigger.py`, `tests/cli/test_analyze.py`.
