# Planning Pre-thinking — GUR-103 Phase 2 Analysis Agent Integration

Surfaces the information assumptions and gaps before writing
`2-plan.md`. Carries forward the three open decisions from
`1-kickoff.md` that the board comment ("Please continue work on
ticket") superseded — they are now **human-accepted gaps with
defaults proposed**, to be re-confirmed at the planning gate.

## To write this plan, I am assuming

### A. Architecture (from research + accepted kickoff)

- **A1.** Four new modules across two layers, no new DB table:
  - `analysis/tools.py` — domain-level `AnalysisTools` (P2-11),
    framework-agnostic. Lives in `analysis/`, not `sdk/`, because
    `read_traces` reads `events_repo`, `read_project_file` does FS
    I/O, `query_structured_store` hits `intelligence.db`, and
    `read_historical_flags` hits `behavior_flags_repo` — none depend
    on PydanticAI. The SDK module *consumes* these by registering
    them as PydanticAI tools.
  - `sdk/__init__.py` + `sdk/agent.py` — concrete `AnalysisAgent`
    impl on PydanticAI (P2-12). Implements the three frozen async
    methods from `analysis/agent.py`. No public surface beyond what
    the Protocol demands.
  - `sdk/router.py` — LLM router (P2-13). Wraps PydanticAI's
    per-model agent with a transport-error fallback chain via
    LiteLLM. Validation errors do NOT trigger fallback.
  - `sdk/model_selection.py` — model selection (P2-14). Pure
    function returning `(primary: ModelSpec, fallbacks:
    list[ModelSpec])` from `(project_config, global_config,
    observation_records)`.
  - `sdk/trigger.py` + observation pipeline subscription +
    `cli/analyze.py` — three trigger paths (P2-15). No new state
    table; uses existing `analysis_runs` (GUR-102 task-1) for
    exactly-once semantics.

- **A2.** `analysis_runs` reuse, not a new table. GUR-102 already
  ships the table with stages
  `pending|segmented|behavior_done|summary_written|aggregated|failed`.
  The orchestrator's `start_run()` inserts at `pending` BEFORE any
  pipeline work begins (DC-1 audit contract). The trigger layer's
  exactly-once check is:
  - `analysis_runs_repo.get_latest_for_session(session_id)` → if
    latest stage is `summary_written` or `aggregated`, skip dispatch
    (without `--force`). If latest stage is non-terminal and
    `updated_at` is recent (< trigger_lock_seconds), skip dispatch.
    Otherwise, dispatch.
  - `start_run` happens inside `orchestrator.analyze_session()` per
    GUR-102 contract; the trigger layer does NOT pre-insert. This
    preserves DC-1 (one row per pipeline execution, inserted by the
    pipeline owner, not the trigger).

- **A3.** Hook integration is **post-ingest**, not in-handler. The
  existing `POST /hook/{event_type}` (`api/hooks.py:111`) already
  uses `asyncio.create_task(pipeline.ingest(event))` — fire-and-
  forget. GUR-103 adds a callback at the pipeline layer:
  `pipeline.ingest()` for events whose `event_type ==
  EventType.SESSION_END` invokes
  `trigger.dispatch_analysis(project_id, session_id)` after the DB
  write succeeds. The hook handler is unmodified — hook latency
  budget is already enforced by GUR-99/GUR-100 contract; GUR-103
  inherits it without touching the handler.

- **A4.** Dependency-injection friendly, mirrors GUR-102. Trigger
  constructor: `Trigger(orchestrator, analysis_runs_repo,
  events_repo, model_selection_fn, lock_registry)`. SDK agent
  constructor: `PydanticAIAnalysisAgent(model_spec, tools,
  router)`. No global state, no module-level config reads. Tests
  inject `FakeRouter` / `FakeOrchestrator` / etc.

### B. Tools layer (`analysis/tools.py`, P2-11)

- **B1.** `read_traces(session_id) -> list[Event]` — thin pass-through
  to `events_repo.get_session_events(session_id)`. No transform.
  Returns `secondsight.observation.Event` objects directly. Tools
  layer does not own normalization.

- **B2.** `read_project_file(project_id, relative_path) -> str` —
  the security-critical method. Resolution rules:
  1. Look up project root from `project_config.root_path`. If
     project_config is missing or `root_path` is unset, raise
     `ProjectFileToolError("project root not configured")`.
  2. Resolve `relative_path` against project root using
     `pathlib.Path.resolve(strict=True)`. If the resolved path does
     not have project root as a prefix (`Path.is_relative_to`),
     raise `ProjectFileToolError("path escapes project root")`
     and log at WARN with `project_id`, `relative_path`,
     `resolved_path`.
  3. Apply denylist match against the resolved path's name and any
     ancestor directory. Default denylist (configurable via
     `project_config.read_project_file.denylist`):
     `[".env", ".env.*", "*credentials*", "*secret*", "*.pem",
     "id_rsa*", ".aws/*", ".ssh/*"]`. Match → raise
     `ProjectFileToolError("path matches denylist pattern: {p}")`
     and log at WARN.
  4. Size cap: 256 KiB. Files larger than the cap return the first
     256 KiB with a truncation marker prepended; do not raise.
  5. UTF-8 decode with `errors='replace'`. Binary files are
     returned as a placeholder (`<binary file: {size}>`); do not
     attempt to read into LLM context.

- **B3.** `query_structured_store(query: StructuredQuery) ->
  list[dict]` — typed query against `intelligence.db`. v1 surface:
  exactly two query shapes — `{"kind":"behavior_flag_summary",
  "project_id":..., "limit":...}` and `{"kind":"directive_active",
  "project_id":..., "limit":...}`. No raw SQL. Each shape maps to
  an existing repo method. Adding new shapes is intentional API
  expansion, not free-form. Death case: a query shape the LLM
  invents (`{"kind":"DROP TABLE"}`) raises `ValueError` and never
  reaches a repo method.

- **B4.** `read_historical_flags(project_id, limit=200) ->
  list[BehaviorFlagSummary]` — pass-through to
  `behavior_flags_repo.get_project_flags_by_type` aggregated by
  flag_type. Returns the most-recent N rows in flag_type-grouped
  shape so the aggregator prompt input fits token budget.

### C. SDK module — PydanticAI agent (`sdk/agent.py`, P2-12)

- **C1.** Concrete class `PydanticAIAnalysisAgent` implementing the
  frozen Protocol. Construction:
  ```python
  class PydanticAIAnalysisAgent:
      def __init__(
          self,
          router: LLMRouter,
          tools: AnalysisTools,
      ) -> None: ...

      async def analyze_segments(
          self, prompts: Sequence[str]
      ) -> list[SegmentAnalysis]: ...

      async def aggregate_flag_type(
          self, prompt: str
      ) -> AggregateOutput: ...

      async def summarize_session(
          self, prompt: str
      ) -> SummaryOutput: ...
  ```

- **C2.** PydanticAI `Agent[Deps, OutputType]` per Protocol method.
  Each method constructs its own Agent with the appropriate
  `output_type` (SegmentAnalysis / AggregateOutput / SummaryOutput)
  so PydanticAI's structured-output validation enforces the schema.
  Tools registered: `read_traces`, `read_project_file`,
  `query_structured_store`, `read_historical_flags` — but tool
  *availability* per method is scoped:
  - `analyze_segments`: `read_traces` + `read_project_file` only
    (segment analysis uses trace + on-demand file content).
  - `aggregate_flag_type`: `read_historical_flags` only.
  - `summarize_session`: `read_traces` + `query_structured_store`.

  This scoping prevents the aggregator from accidentally reading
  raw project files (out-of-scope for cross-session pattern
  analysis), and prevents `summarize_session` from hitting the
  filesystem when its job is to summarize known segment results.

- **C3.** Batched form (`analyze_segments(prompts)`) runs sequentially
  via PydanticAI for v1 — one Agent invocation per prompt. List
  length contract: `len(out) == len(in)`. Any single failure raises
  `AnalysisAgentError` with the prompt index in the message and
  stops the batch. (Per GUR-102 Protocol contract: no partial
  success.) Concurrent batching is a nice-to-have, not v1.

### D. SDK module — LLM Router (`sdk/router.py`, P2-13)

- **D1.** Router contract:
  ```python
  class LLMRouter:
      def __init__(
          self,
          primary: ModelSpec,
          fallbacks: list[ModelSpec],
          per_call_timeout_s: float = 60.0,
          chain_total_timeout_s: float = 90.0,
      ) -> None: ...

      async def call(
          self,
          *,
          model_input: ModelMessage,  # PydanticAI message shape
          output_type: type[T],
      ) -> T: ...
  ```

- **D2.** Failure-mode allowlist for fallback (the cost-leak
  control). Fallback fires ONLY on:
  - `httpx.TimeoutException` and subclasses
  - `httpx.ConnectError`, `httpx.RemoteProtocolError`
  - LiteLLM/PydanticAI exceptions matching `RateLimitError`,
    `APIConnectionError`, `ServiceUnavailableError`,
    `InternalServerError` (5xx family)
  - `ProviderAuthError` (4xx auth) — fallback once per chain (the
    fallback model may use a different provider with valid auth)
  Fallback does NOT fire on:
  - `pydantic.ValidationError` from output validation
  - `pydantic_ai.UnexpectedModelBehavior` when caused by schema
    mismatch (model returned syntactically valid JSON but failed
    schema validation)
  - `AnalysisAgentError` raised inside tool methods (project file
    not found, denylist hit, etc.)
  These bubble as `AnalysisAgentError` to the orchestrator.

- **D3.** Logging contract per call attempt:
  `logging.info(provider, model, tokens_in, tokens_out, duration_ms,
  attempt, total_attempts, outcome={success|fallback_triggered|
  validation_error|unrecoverable})`. The aggregator's North Star
  corruption signature ("fallback fires on >50% of calls") relies
  on these log lines being machine-parseable.

- **D4.** Chain exhaustion: when all fallbacks fail with
  fallback-eligible errors, raise `AnalysisAgentError("all chain
  models failed", attempts=[...])`. The orchestrator catches this
  and writes `analysis_runs.stage='failed'` with the chain trace
  in `error_message`. No silent degradation.

### E. SDK module — Model selection (`sdk/model_selection.py`, P2-14)

- **E1.** Pure function:
  ```python
  def select_model(
      project_id: str,
      project_config: ProjectConfig,
      global_config: GlobalAnalysisConfig,
      events_repo: EventsRepository,
  ) -> tuple[ModelSpec, list[ModelSpec]]:
      ...
  ```
  Returns `(primary, fallbacks)`. Pure: no side effects, no
  network, no random.

- **E2.** Resolution order (first match wins):
  1. Project config `analysis.model` non-empty → primary = that
     spec, fallbacks = global config `analysis.models.fallback.
     fallback_models`.
  2. Global config `analysis.models.<inferred_agent_type>` is a
     concrete model name → primary = that, fallbacks = global
     fallback list.
  3. Global config `analysis.models.<inferred_agent_type> ==
     "auto"` → primary = adapter-default for that agent_type
     (table from SD §5.7.1: `claude_code → claude-haiku-4-5`,
     `codex → "auto"-not-yet-configured` raises
     `ModelSelectionError`, `opencode → ""` raises
     `ModelSelectionError("opencode requires explicit
     analysis.model")`). Fallbacks = global fallback list.
  4. No config match → `ModelSelectionError`.

- **E3.** `agent_type` inference policy (the `auto`-flag question
  carried from research). Only invoked when `default_agent ==
  "auto"`. Inferred via
  `events_repo.get_latest_session_agent_type(project_id)` — the
  most-recent session's `agent_type` column. **`auto` is opt-in**;
  default is `default_agent = "claude_code"` (the SD §5.7.1 default
  rationale: most-supported agent, cheapest model). When `auto` is
  set and inference returns `None` (no events), fall back to
  `claude_code`. Logged at INFO whenever inference changes the
  selected model from a previous session.

### F. Trigger layer (`sdk/trigger.py` + pipeline subscription, P2-15)

- **F1.** Three trigger entrypoints, all calling a single
  `Trigger.dispatch(project_id, session_id, *,
  source: Literal["event"|"timeout"|"manual"], force: bool=False)`
  method:
  - `pipeline.ingest()` post-write callback for
    `EventType.SESSION_END`.
  - Periodic sweeper (`sweep_stale_sessions()`) running on the API
    server event loop every `analysis.sweep_interval_seconds`
    (default 60s).
  - `secondsight analyze` Typer subcommand.

- **F2.** `dispatch()` exactly-once contract:
  1. Acquire `lock_registry.session_lock(session_id)` —
     asyncio.Lock per session_id, created on first use, kept in a
     weakref dict so completed sessions release. If `force=False`
     and the lock is held, `dispatch` returns immediately (some
     other path is already dispatching).
  2. Inside the lock: query `analysis_runs_repo.
     get_latest_for_session(session_id)`. If latest stage is
     `aggregated` or `summary_written` and `force=False`, return
     `DispatchResult(dispatched=False, reason="already-analyzed",
     run_id=existing_id)`.
  3. If latest stage is non-terminal and `(now - updated_at) <
     trigger_lock_seconds` (default 30s), return `dispatched=False,
     reason="another-run-in-flight"`.
  4. Otherwise, schedule
     `asyncio.create_task(orchestrator.analyze_and_aggregate(
     session_id))`. Return `dispatched=True, run_id=None` (the
     orchestrator's `start_run()` will create the row).

- **F3.** Sweeper contract:
  - Every `sweep_interval_seconds`, query for sessions where:
    `events.event_type='session_end' is NOT present` AND
    `last_event_ts < now() - session_timeout_minutes` AND
    `analysis_runs.session_id is NULL OR latest_stage NOT IN
    (summary_written, aggregated)`.
  - For each match, call `dispatch(source="timeout")`.
  - The sweep runs as a long-lived asyncio task started by the
    API server's lifespan startup hook; cancelled on shutdown.

- **F4.** Manual CLI:
  - `secondsight analyze [--session SESSION_ID] [--force]
    [--project PROJECT_ID]`.
  - No SESSION_ID → pick the most recent session for the project
    where `latest_stage NOT IN (summary_written, aggregated)`.
  - With `--force`, always dispatches; bypasses the
    "already-analyzed" check inside `dispatch`.
  - Foreground: prints `[INFO]` log lines as the orchestrator
    progresses through stages (segmented → behavior_done →
    summary_written → aggregated). Exit code 0 on terminal-success
    stage, 1 on `failed`.
  - Talks to the API server via `httpx` (server mode) OR runs the
    orchestrator in-process if the server isn't running. The
    decision is `try server, fallback to in-process` — logged at
    INFO so users know which path ran.

### G. Hook latency budget

- **G1.** GUR-103 must NOT add code to `api/hooks.py:handle_hook`.
  All trigger work happens after `pipeline.ingest()` has written
  the event to DB — i.e., already inside a `create_task` from the
  hook handler's perspective. Hook latency budget (already enforced
  by Phase 1) is unchanged.
- **G2.** The post-ingest callback for SESSION_END runs inside
  `pipeline.ingest()`. It MUST be fire-and-forget
  (`asyncio.create_task`) — never `await`-ed inside `ingest` —
  so a slow `dispatch` does not block subsequent event ingestion.

### H. Validation gates

- **H1.** Phase 2 ship gate (this issue): `pytest tests/sdk/
  tests/analysis/test_tools.py tests/cli/test_analyze.py -v` is
  100% green deterministically. End-to-end smoke test:
  feed an `events` table with a complete session, call
  `secondsight analyze --session <id>`, observe `behavior_flags`
  rows + `directives` rows + `analysis_runs.stage='aggregated'`.
- **H2.** Promoted from Phase 3 (deferred validation, ship-manifest
  notes only): `read_project_file_path_escape_attempts == 0`
  across the first 20 dogfooding sessions. Tied as a blocker to
  any "make read_project_file default-on" follow-up.
- **H3.** Promoted from Phase 3 (deferred): fallback chain success
  rate ≥ 0.95 when the chain is exercised. Measured from router
  log lines.

## Gaps I cannot resolve from Research (carried forward from research gate)

These three were raised in `1-kickoff.md` and the `research`
issue document. The board's "Please continue work on ticket"
comment superseded the research-stage confirmation, which I
interpret as **proceed with documented defaults**. Below: the
defaults pinned for v1, with rationale, plus the explicit mark
that they are accepted gaps to re-confirm at the planning gate.

### G1. Fallback chain default (`analysis.models.fallback.fallback_models`)

**Default pinned for v1:** `["gpt-4o-mini", "gemini-2.0-flash"]`
(matches SD §5.7.2 example).

Rationale: provider diversity (OpenAI + Google) maximizes the
probability that a regional outage on one provider leaves the
chain functional. Both are cheap (sub-Haiku-tier) so a fallback
event does not multiply spend by an order of magnitude. SD already
ratifies this list as the example default.

Risk if wrong: Users without `OPENAI_API_KEY` and without
`GOOGLE_API_KEY` get an empty fallback chain → all primary
failures become hard `AnalysisAgentError` → analyses fail for
that session. Mitigated by: **router logs at WARN when fallback
chain is empty at construction time**, surfacing the condition at
server startup rather than at first failure.

Re-confirm at planning gate: yes.

### G2. `auto` model-selection semantics

**Default pinned for v1:** `default_agent` config key defaults to
`"claude_code"` (the most-supported adapter, cheapest tier).
`auto` inference is **opt-in** — users explicitly write
`default_agent = "auto"` to enable observation-record-based
inference.

Rationale: the inverse default ("auto by default, override by
config") creates the silent surprise of changing model on adapter
swap. Pinning `claude_code` as the explicit default makes the
config diff visible when a user wants the auto behavior. SD §5.7.2
example config has `default_agent = "auto"`, but that line is the
*example*, not the *default*; SD §5.7.1 reasoning ("Anthropic
體系最輕量") supports `claude_code` as a sensible default.

Risk if wrong: users on Codex or OpenCode whose config doesn't
set `default_agent` explicitly get `claude_code` selected, which
fails at model-resolution if they have no Anthropic key. Mitigated
by: **`select_model` raises `ModelSelectionError` with the
config diff that would fix it** ("set
`[analysis] default_agent = "codex"` or
`[analysis.models.codex] = "<model-name>"`").

Re-confirm at planning gate: yes.

### G3. `read_project_file` default-deny denylist

**Default pinned for v1:** denylist applied unconditionally with
the patterns in section B2 above. **No global feature-flag** for
the whole tool; the tool ships available with sandbox + denylist
both active. Project config can extend the denylist
(`[analysis.read_project_file] denylist = [...]`) but cannot
disable it.

Rationale: the alternative (feature-flag the entire tool until
v2) would block the LLM from doing on-demand file verification —
the explicit reason SD §5.4 introduced the tool ("LLM 判斷不確定
時，透過 `read_project_file` 讀取實際檔案驗證"). The damage from
losing the tool is concrete: every uncertain segment becomes a
false negative. The damage from a leaky tool is also concrete: a
secret leaks to a third-party LLM. The denylist + sandbox is the
balance — narrow tool surface, default-deny on the obvious leak
patterns, log-and-WARN on every escape attempt.

Risk if wrong: a denylist gap allows leakage. Mitigated by:
**WARN-level log on every reject AND every successful read** of
a path matching `*.env*|*key*|*secret*` (i.e., even when allowed
by an explicit project config override, log the read so dogfooding
audit can spot misconfiguration).

Re-confirm at planning gate: yes — and explicitly: should we ship
a global feature-flag (`[analysis.read_project_file] enabled =
false` opt-out) so paranoid users can disable the tool entirely?
Default proposal: yes, ship the opt-out. Costs ~10 lines, gives
users a safety valve.

## Gaps I cannot resolve from Research (new — surfaced during pre-thinking)

### G4. `query_structured_store` query shape vocabulary

The tool's v1 shape (B3) admits exactly two `kind` values
(`behavior_flag_summary`, `directive_active`). SD §5.4 names the
tool but does not enumerate query shapes. **Question for board:**
acceptable to ship v1 with these two only, expanding via explicit
PRs as new analysis prompts need new lookups? Or should v1
support a wider shape vocabulary up front
(e.g., `session_metadata`, `segment_count_by_session`,
`directive_history`)?

Default pinned: ship the two shapes. Each new shape is an
intentional API expansion. New shapes require: type addition to
`StructuredQuery`, repo method behind it, test coverage. The
small surface stays auditable; the LLM's structured-output
schema for tool calls only allows the listed shapes (PydanticAI's
output validation enforces this).

### G5. Fallback budget when project_config disables fallback

Some users may want strict cost control: "use my chosen primary
or fail loud, never fall back." **Question for board:**
acceptable to ship v1 with no per-project override (everyone gets
the global fallback chain)? Or do we add
`[analysis.models.fallback] enabled = true|false` per project?

Default pinned: ship the global-only chain. Per-project
fallback override is a Phase 3+ feature; users who want strict
control today can set `analysis.models.fallback.fallback_models =
[]` globally (router constructed with empty fallbacks logs WARN
but proceeds — primary failure raises `AnalysisAgentError`
immediately).

### G6. Sweeper interaction with API server lifecycle

The periodic sweeper runs as a long-lived asyncio task started by
the lifespan startup hook. **Question for board:** when no API
server is running (user has SecondSight installed but `serve` is
not active), the timeout-fallback trigger is silently inactive.
Manual CLI still works, event-driven trigger still works (the
hook handler that publishes events also depends on the server,
so the absence of the sweeper on a server-less install is
self-consistent). Is this the right semantics, or should the
sweeper be a separate process / cron job?

Default pinned: keep sweeper in the API server lifespan. SD §5.6
says timeout-fallback is "for when the user closes the terminal
without a clean session_end" — and the API server is what
receives session-end hooks anyway, so its presence is the
common case. Documented as a "known limitation: timeout-based
analysis requires `secondsight serve` to be running."

## Uncertainties

### U1. PydanticAI version pinning

The repo's `pyproject.toml` does not yet depend on `pydantic-ai`
or `litellm`. The current PydanticAI API expects `pydantic-ai >=
0.0.x` (pre-1.0; API still subject to change). If the version we
pin during planning has its API shift mid-Phase-3, the SDK module
could need rework. **Resolution:** pin to the latest stable
version at planning time and note in `2-plan.md` that any
PydanticAI minor-version bump triggers a regression test pass.
Not a blocker.

### U2. LiteLLM as a PydanticAI provider

PydanticAI supports OpenAI-compatible endpoints natively; LiteLLM
exposes an OpenAI-compatible proxy. The router contract (D1) can
either:
(a) call PydanticAI directly per provider and implement fallback
in `LLMRouter` ourselves (simpler stack, more code in our repo); or
(b) point PydanticAI at LiteLLM as the single provider, let
LiteLLM handle multi-provider + fallback (less code in our repo,
adds runtime dependency on LiteLLM proxy semantics).

**Resolution:** option (a) for v1 (own the fallback policy fully —
critical because the failure-mode allowlist in D2 is the cost
control). LiteLLM is referenced only as a *provider option* for
PydanticAI in cases where a user needs a non-OpenAI-compatible
provider (matches SD §5.2 phrasing: "特殊 provider 可透過 LiteLLM
作為 provider fallback 掛載"). Documented in `2-plan.md`.

## Output state

- Gaps: **6** (G1, G2, G3 carried from research with proposed
  defaults; G4, G5, G6 newly surfaced with proposed defaults).
  All proposed defaults documented above. **All to be re-confirmed
  at the planning gate.**
- Uncertainties: **2 resolved** (U1 version pinning, U2 LiteLLM
  positioning).

## Transition

Pre-thinking artifact ready. Plan content (`2-plan.md`,
`acceptance.yaml`, `overview.md`, `index.yaml`, 5 task files)
will be written next, treating G1–G6 as undocumented assumptions
that the planning-gate confirmation explicitly ratifies (or
overrides) before implementation begins.
