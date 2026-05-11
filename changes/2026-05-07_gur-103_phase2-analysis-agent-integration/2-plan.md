# Plan: GUR-103 Phase 2 Analysis Agent Integration

**Inputs:** `1-kickoff.md`, `problem-autopsy.md`, `2-pre-thinking.md`.
**Status of pre-thinking gate:** `accept_with_carried_gaps` —
the board's comment `3d50b8dc` ("Please continue work on ticket")
superseded the research-stage confirmation
(`529e7124`, supersedeOnUserComment=true). Six gaps (G1–G6) are
carried into this plan as undocumented assumptions with proposed
defaults, to be re-confirmed at the planning gate.

## 1. Feature description

Bind the existing Phase 2 orchestration pipeline (GUR-102:
`Orchestrator.analyze_session` / `aggregate_project` /
`analyze_and_aggregate`) to a real LLM, and give that pipeline
three trigger paths so it actually runs.

Five new files across two layers, no new DB tables, no schema
changes:

- `analysis/tools.py` — `AnalysisTools` (P2-11), domain-level.
- `sdk/__init__.py` + `sdk/agent.py` — `PydanticAIAnalysisAgent`
  implementing the frozen Protocol (P2-12).
- `sdk/router.py` — `LLMRouter` with transport-error fallback (P2-13).
- `sdk/model_selection.py` — pure model-selection function (P2-14).
- `sdk/trigger.py` + `cli/analyze.py` + a one-line subscription in
  `observation/pipeline.py` — three trigger paths (P2-15).

## 2. Ratified decisions (D-numbered, pinned from pre-thinking)

- **D1.** Tools layer lives in `analysis/`, not `sdk/`. Tools are
  framework-agnostic; SDK consumes them. (Pre-thinking A1.)
- **D2.** Reuse existing `analysis_runs` table (GUR-102 task-1) for
  trigger dedup. No new table. (Pre-thinking A2.)
- **D3.** Hook handler is **not modified**. Trigger dispatch happens
  inside `pipeline.ingest()` after the DB write succeeds, via an
  `asyncio.create_task` so ingest itself is unblocked. (A3, G1, G2.)
- **D4.** Per-method tool scoping: `analyze_segments` sees
  `read_traces` + `read_project_file`; `aggregate_flag_type` sees
  only `read_historical_flags`; `summarize_session` sees
  `read_traces` + `query_structured_store`. The aggregator
  literally cannot read raw project files. (Pre-thinking C2.)
- **D5.** Router fallback fires only on the transport-error
  allowlist (Timeout, ConnectError, RemoteProtocolError,
  RateLimitError, APIConnectionError, ServiceUnavailableError,
  5xx, ProviderAuthError-once-per-chain). `pydantic.
  ValidationError` and `pydantic_ai.UnexpectedModelBehavior`
  bubble as `AnalysisAgentError` and never trigger fallback. This
  is the cost-leak control. (D2 in pre-thinking.)
- **D6.** Direct PydanticAI provider per model; LiteLLM is the
  *escape hatch* for non-OpenAI-compatible providers, NOT the
  router. We own the fallback policy fully. (Pre-thinking U2.)
- **D7.** `auto` is **opt-in**; default `default_agent =
  "claude_code"`. Pinning the explicit default avoids silent
  model-swap on adapter change. (Pre-thinking E3 / G2.)
- **D8.** `read_project_file` ships available with sandbox +
  default-deny denylist both active. Project config can extend
  the denylist (additive only; cannot disable). Optional
  per-project tool-disable feature flag
  (`[analysis.read_project_file] enabled = false`). (G3.)
- **D9.** Manual CLI prefers server-mode (HTTPX → API server) and
  falls back to in-process orchestrator if the server is down.
  Path taken is logged at INFO. (Pre-thinking F4.)
- **D10.** Sweeper runs as an asyncio task in the API server's
  lifespan; not a separate process. Documented as "timeout-based
  analysis requires `secondsight serve`." (G6.)
- **D11.** Fallback chain default `["gpt-4o-mini",
  "gemini-2.0-flash"]`, matching SD §5.7.2. Empty fallback chain
  logs WARN at construction. (G1.)
- **D12.** `query_structured_store` v1 supports exactly two
  shapes: `behavior_flag_summary`, `directive_active`. New
  shapes are intentional API expansion. (G4.)
- **D13.** Per-project fallback override deferred. v1 has only
  the global chain; users who want strict control set
  `fallback_models = []` globally. (G5.)
- **D14.** Single source of truth for trigger dedup is
  `analysis_runs.get_latest_for_session`. The trigger layer
  does NOT pre-insert a row; the orchestrator's existing
  `start_run()` does (preserves DC-1 audit contract). (Pre-thinking F2.)

## 3. Death cases (DC-numbered)

These are the silent-failure surfaces this issue must close. Each
gets at least one death test (DT-N.M) in `acceptance.yaml`.

- **DC-1: Sandbox bypass via symlink.** A project root contains a
  symlink pointing outside the root. `pathlib.Path.resolve()`
  follows it; `is_relative_to(project_root)` returns False on
  the resolved path, but if we used `Path(project_root /
  user_path).resolve()` without `strict=True`, a non-existent
  path could pass through resolve() with the unresolved string.
  Lie: "looks like a normal in-project read." Truth: file
  content from outside the project is sent to LLM. Detection:
  every resolution computes the post-`resolve(strict=True)` path
  and re-checks `is_relative_to`; rejection logged at WARN with
  the resolved path.

- **DC-2: Denylist bypass via path-component case.** User's
  filesystem has `.ENV` (uppercase). On case-insensitive
  filesystems (macOS default, NTFS) it points to the same file
  as `.env`; on case-sensitive filesystems (Linux ext4) it's a
  different file. Lie: "denylist matched, file blocked." Truth:
  on Linux, `.ENV` is a *different* file holding the same secret
  (user copied it intentionally) and the literal-pattern match
  (`*.env`) misses it. Detection: denylist match is
  case-insensitive on filename component, regardless of FS;
  WARN-log preserves the original path.

- **DC-3: Validation error masquerading as transport error.**
  LiteLLM/PydanticAI wraps a `ValidationError` inside a generic
  exception class (e.g., `UnexpectedModelBehavior`) that the
  router's allowlist matches by class name. Lie: "transient
  failure, retried, fallback fixed it." Truth: the prompt is
  malformed; every model in the chain returns the same broken
  output; we just paid 3× for the same failure. Detection:
  router unwraps `__cause__` chain and inspects the *root* cause
  before deciding fallback eligibility; `ValidationError` at any
  depth = no fallback.

- **DC-4: Trigger race produces duplicate analyze_session calls.**
  Event-driven trigger and timeout sweeper both fire for the
  same session within milliseconds (sweeper just woke up, then
  session_end arrives). Both pass the
  `get_latest_for_session` check (no row exists yet). Both
  schedule `orchestrator.analyze_session()` tasks. Lie:
  "succeeded with N flag rows." Truth: 2N flag rows
  (orchestrator's `INSERT OR IGNORE` deduplicates row identity
  but per-segment LLM calls happen twice; cost doubled). Mitigation:
  `lock_registry.session_lock` per session_id, acquired non-blocking
  (`acquire(blocking=False)` semantics via `try-except-not-acquired`).

- **DC-5: Hook handler latency leak via shared event loop.** The
  post-ingest dispatch is `asyncio.create_task` inside
  `pipeline.ingest`, but the dispatched task does CPU-bound work
  (e.g., synchronous `Path.read_bytes()` for read_project_file
  inside the LLM tool callback). The event loop blocks while
  read_bytes runs; concurrent hook handlers queue. Lie: "hook
  latency p95 unchanged." Truth: under load, hook latency
  doubles. Detection: tool methods that touch FS or DB use
  `await asyncio.to_thread(...)`; verified by a stress test
  that runs N concurrent hooks during one analysis dispatch.

- **DC-6: Sweeper's "session has no session_end event" check
  treats Phase 1 backfill state as missing.** `events_repo` has
  in-flight backfill work; sweeper queries see a session whose
  session_end event is in the backfill queue but not yet
  written. Lie: "session timed out, dispatching." Truth: the
  session is still actively reporting events; we triggered an
  analysis on partial data. Detection: sweeper queries the
  `events_repo` for `last_event_ts` (most recent ANY event,
  not just session_end) and only dispatches if `last_event_ts <
  now() - timeout`. The "no session_end" condition is a
  *secondary* filter, not the primary trigger.

- **DC-7: Manual CLI silently succeeds on a stale already-completed
  session.** User runs `secondsight analyze --session OLD_ID`
  expecting a re-run; CLI sees `summary_written` and exits 0
  with no output. Lie: "analysis ran." Truth: nothing happened;
  user thinks they have fresh data. Detection: when CLI's
  `dispatch()` returns `dispatched=False, reason="already-
  analyzed"`, CLI prints an explicit "skipped, already analyzed
  on YYYY-MM-DD; pass `--force` to re-run" message and exits
  with code 2 (distinct from success).

- **DC-8: Empty-fallback-chain config silently raises on first
  primary failure.** User explicitly sets `fallback_models = []`
  (D13's strict-mode escape valve). Router constructed; primary
  fails with a transport error; chain has no models to try; an
  unwrapped exception bubbles. Lie: "router failed mid-call."
  Truth: this is the expected behavior of an empty chain, but
  the error message doesn't say so — user sees a generic
  `httpx.TimeoutException` and thinks the router is broken.
  Detection: empty chain at construction logs WARN; router's
  `AnalysisAgentError` on chain exhaustion includes the chain
  configuration in the message ("primary timed out;
  fallback_models is empty by config — no retry attempted").

## 4. Module headlines (MH-numbered, map 1:1 to tasks)

- **MH-1 (task-1, P2-11): `analysis/tools.py`** — Implements
  `AnalysisTools` with the four methods (B1–B4 in pre-thinking).
  Closes DC-1, DC-2.
- **MH-2 (task-2, P2-13): `sdk/router.py`** — `LLMRouter` with the
  failure-mode allowlist (D5, D6). Closes DC-3, DC-8.
- **MH-3 (task-3, P2-14): `sdk/model_selection.py`** — Pure
  `select_model()` function (E1–E3). No death cases (pure
  function, well-typed inputs).
- **MH-4 (task-4, P2-12): `sdk/agent.py`** — `PydanticAIAnalysisAgent`
  binding the Protocol to per-method PydanticAI Agents with
  scoped tool availability (C1–C3, D4). No new death cases
  (relies on MH-1/2/3 controls).
- **MH-5 (task-5, P2-15): `sdk/trigger.py` + `cli/analyze.py` +
  pipeline subscription** — Three trigger paths converging on
  `Trigger.dispatch()` with per-session lock (F1–F4). Closes
  DC-4, DC-5, DC-6, DC-7.

## 5. Cross-issue contracts

GUR-103 honors three contracts produced by GUR-100/101/102:

- **`AnalysisAgent` Protocol shape** (`analysis/agent.py`,
  GUR-102). Frozen. GUR-103 implements; does not modify.
- **`analysis_runs` audit contract** (`storage/
  analysis_runs_repository.py`, GUR-102). The orchestrator owns
  `start_run()`; trigger layer does NOT pre-insert.
- **Pipeline ingest contract** (`observation/pipeline.py`,
  GUR-99). GUR-103 adds a post-write callback for
  `EventType.SESSION_END`; does not modify ingest semantics.

## 6. Configuration additions

New keys under `[analysis]`:

```toml
[analysis]
default_agent = "claude_code"   # D7; "auto" is opt-in
sweep_interval_seconds = 60     # D10; how often the sweeper runs
session_timeout_minutes = 30    # SD §5.6 default
trigger_lock_seconds = 30       # F2 step 3 dedup window

[analysis.models.fallback]
fallback_models = ["gpt-4o-mini", "gemini-2.0-flash"]  # D11

[analysis.read_project_file]
enabled = true                  # D8 escape valve
denylist = []                   # additive on top of built-in
size_cap_kb = 256               # B2 step 4

[analysis.router]
per_call_timeout_s = 60.0       # D1 in pre-thinking
chain_total_timeout_s = 90.0
```

Config schema lives in `analysis/config.py` (new), modeled on
`storage/retention_config.py` (GUR-147 pattern). TOML loader,
Pydantic model, default-value tests.

## 7. Evidence (citations from current repo state)

- `analysis/agent.py:30-72` — Protocol body, three async methods,
  `AnalysisAgentError` exception. GUR-103 imports from here.
- `storage/analysis_runs_repository.py:59-85` —
  `start_run()` is the only path that creates rows. Trigger
  layer must not duplicate.
- `storage/analysis_runs_repository.py:156-178` —
  `get_latest_for_session()` is the dedup source of truth.
- `api/hooks.py:111-217` — Hook handler is already
  `asyncio.create_task(pipeline.ingest(event))`. We do not modify.
- `adapters/claude_code.py:63` — `SessionEnd → EventType.
  SESSION_END` mapping confirmed; pipeline already receives
  these events.
- `cli/app.py` — Typer app entry; `analyze` subcommand registers
  here.
- `docs/system_design.md:711-1060` — SD sections 5.2/5.4/5.6/
  5.7.1/5.7.3/5.7.4 cited as the authoritative source.

## 8. Out of scope (re-stated for the planning gate)

- CLI mode agent (SD §5.7.3 — borrowing user's coding-agent).
  SDK mode only.
- Directive lifecycle transitions. → GUR-104.
- Dashboard rendering. → GUR-106.
- Cross-adapter session-end normalization. Adapters either emit
  `EventType.SESSION_END` or fall back to timeout-only.
- Concurrent per-segment LLM calls (sequential is fine v1).
- Persistent fallback-event telemetry (logging.info is enough v1).

## 9. Test plan summary

`tests/analysis/test_tools.py` (MH-1):
- DT-1.1 sandbox rejects symlink escape
- DT-1.2 denylist match is case-insensitive on filename
- DT-1.3 denylist match on ancestor directory (`.ssh/id_rsa`)
- DT-1.4 size cap truncates with marker
- DT-1.5 binary file returns placeholder, not raw bytes
- DT-1.6 `query_structured_store` rejects unknown `kind`
- DT-1.7 happy-path read_traces returns Event objects

`tests/sdk/test_router.py` (MH-2):
- DT-2.1 ValidationError at any cause-chain depth → no fallback
- DT-2.2 transport error → fallback fires
- DT-2.3 chain exhaustion → AnalysisAgentError with chain trace
- DT-2.4 empty chain at construction → WARN log
- DT-2.5 chain_total_timeout enforcement
- HP-2.6 single primary success: zero fallback events

`tests/sdk/test_model_selection.py` (MH-3):
- DT-3.1 missing config raises ModelSelectionError with config
  diff suggestion
- DT-3.2 `auto` with no events → falls back to claude_code
- DT-3.3 project override beats global
- HP-3.4 `auto` + recent claude_code session → claude-haiku-4-5

`tests/sdk/test_agent.py` (MH-4):
- DT-4.1 `aggregate_flag_type` calls cannot reach
  `read_project_file` (ToolNotAvailableError)
- DT-4.2 `analyze_segments` partial-batch failure raises with
  prompt index
- HP-4.3 single-segment golden path with FakeRouter

`tests/sdk/test_trigger.py` + `tests/cli/test_analyze.py` (MH-5):
- DT-5.1 (DC-4) concurrent dispatch on same session: only one
  `analyze_and_aggregate` task is scheduled
- DT-5.2 (DC-5) FS-blocking tool wrapped in `to_thread`
- DT-5.3 (DC-6) sweeper uses `last_event_ts` not just `session_end`
- DT-5.4 (DC-7) manual CLI on completed session exits with code 2
  and explicit message
- HP-5.5 happy-path event-driven dispatch records run_id
- HP-5.6 sweeper happy-path on a session with last_event_ts older
  than timeout

## 10. Risks unmitigated by this plan

- **PydanticAI minor-version churn** (U1). Pinned at planning;
  bumps trigger regression test pass. Documented.
- **Single asyncio event loop shared with hooks/dashboard.** D5 +
  DC-5 control mitigates blocking work; doesn't eliminate
  long-running awaits. The North Star sub-metric on dashboard
  polling latency catches degradation.
- **Empty fallback chain + strict primary** (DC-8). Documented in
  config rationale.
