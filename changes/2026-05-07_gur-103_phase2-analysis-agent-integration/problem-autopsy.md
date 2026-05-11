# Problem Autopsy: gur-103-phase2-analysis-agent-integration

## original_statement

> Wire the analysis system to an agent framework with model routing.
>
> **Tasks (P2-11 to P2-15):**
> - P2-11: Analysis Agent tools — `AnalysisTools`: `read_traces`,
>   `read_project_file`, `query_structured_store`,
>   `read_historical_flags`
> - P2-12: PydanticAI agent scaffold (SDK mode) — `sdk/` module:
>   PydanticAI-based agent loop + tools binding
> - P2-13: LLM Router — primary model → fallback models routing via
>   LiteLLM
> - P2-14: Analysis model selection — from config + observation records
>   → infer agent_type → select model
> - P2-15: Analysis trigger mechanism — session end event → auto-trigger
>   background analysis; timeout-based fallback;
>   `secondsight analyze` manual trigger
>
> **Exit criteria:**
> - SDK mode agent loop runs with PydanticAI
> - LLM Router can fall back across models
> - Auto-trigger works on session end (non-blocking)
>
> **Ref:** SD 5.2, 5.4, 5.6, 5.7.1, 5.7.3, 5.7.4

## reframed_statement

GUR-103 binds the existing orchestration pipeline to an actual LLM and
gives that pipeline a way to be reached. The work splits into four
disjoint surfaces, none of which overlap with prior issues:

1. **Tools layer (`analysis/tools.py`)** — domain-level adapters that
   read from the trace store, project filesystem, intelligence DB,
   and behavior_flags history. Independent of agent framework. Used
   by any agent implementation (SDK or hypothetical future CLI mode).
2. **SDK module (`sdk/agent.py` + `sdk/router.py` + `sdk/model_selection.py`)**
   — PydanticAI-backed concrete implementation of the
   `AnalysisAgent` Protocol, with LiteLLM-driven primary→fallback
   routing and a model-selection function that reads config + the
   observation record's `agent_type`.
3. **Trigger layer (`sdk/trigger.py` + observation hook subscription
   + `cli/analyze.py`)** — three paths into `analyze_session()`:
   event-driven, timeout-fallback, manual CLI.
4. **State layer (`analysis_runs` table)** — single source of truth
   for "has this session been analyzed?" so all three trigger paths
   converge on exactly-once semantics.

Phase 2 model invocation, prompt-builder coupling to a specific LLM
client, and CLI mode (borrowing the user's coding-agent binary) are
deliberately not in scope here.

## translation_delta

```yaml
translation_delta:
  - original: "P2-11: Analysis Agent tools — AnalysisTools (in GUR-103, an SDK issue)"
    reframed: "AnalysisTools lives in analysis/tools.py — agent-framework-agnostic, not in sdk/"
    delta: |
      Original phrasing groups the tools under the SDK issue, which
      reads as if they belong inside sdk/. They don't: the tools are
      domain logic (read_traces queries the traces repo;
      read_project_file does FS I/O; query_structured_store hits
      intelligence.db; read_historical_flags hits the behavior_flags
      repo). None of them depend on PydanticAI. The SDK module
      *consumes* them by registering them as PydanticAI tools, but
      the implementation belongs at the analysis layer. SD §5.4
      defines them in the analysis section, not the SDK section, which
      supports this placement.

  - original: "P2-12: PydanticAI agent scaffold (SDK mode) — sdk/ module: PydanticAI-based agent loop + tools binding"
    reframed: "sdk/agent.py implements the AnalysisAgent Protocol frozen by GUR-102 in analysis/agent.py — three async methods bound to PydanticAI Agents with structured output types"
    delta: |
      Original is silent on the contract this scaffold must satisfy.
      That contract is non-negotiable: the Protocol in
      analysis/agent.py is locked. The scaffold's "scaffold-ness"
      is bounded by exactly three async method signatures and three
      Pydantic output types. Reframing makes the constraint explicit
      so we don't ship a "scaffold" that adds public surface area
      beyond what the Protocol demands.

  - original: "P2-13: LLM Router — primary model → fallback models routing via LiteLLM"
    reframed: "Router in sdk/router.py: catches Timeout/RateLimit/ProviderError/APIConnectionError on primary call and walks fallback_models in config order. Validation errors do NOT trigger fallback (they bubble to orchestrator as AnalysisAgentError)."
    delta: |
      Original is silent on which exceptions trigger fallback. This is
      load-bearing: if validation errors (malformed JSON, schema
      mismatch) trigger fallback, we'll quietly multiply spend by
      retrying broken prompts on every model in the chain. Pinning
      the failure-mode list now prevents that silent cost leak.
      LiteLLM's default fallback policy is broad and includes
      validation errors in some configurations — we explicitly
      narrow it.

  - original: "P2-14: Analysis model selection — from config + observation records → infer agent_type → select model"
    reframed: "model_selection() returns (primary, fallbacks) given (a) project config override, (b) global config, (c) most-recent session's agent_type when global config = 'auto'. agent_type inference applies only when config explicitly opts in via 'auto'; default behavior is 'use whatever config says, ignore observation'."
    delta: |
      Original phrasing implies inference is the default path. SD
      §5.7.2 default config has explicit per-agent_type model
      mappings (claude_code → haiku, codex → auto, opencode → "").
      "auto" is opt-in, not default. Treating inference as default
      would silently change a user's model on adapter switch, which
      is a surprise. Pinning "config is authoritative; auto is opt-in"
      avoids that surprise.

  - original: "P2-15: session end event → auto-trigger background analysis; timeout-based fallback; secondsight analyze manual trigger"
    reframed: |
      Three trigger paths converge on exactly-once
      analyze_session() via a new analysis_runs status table:
      (1) event-driven via observation hook subscription,
      (2) periodic timeout sweeper for sessions without
      session_end events,
      (3) Typer CLI subcommand. All three check
      analysis_runs.status before dispatching.
    delta: |
      Original lists three triggers but doesn't address the
      coordination problem: what happens when the event-driven
      trigger and the timeout sweeper both fire for the same session
      (e.g., session_end event arrives just after the sweeper
      synthesized one)? Without a shared state table, we'd run
      analyze_session twice and rely on the orchestrator's INSERT OR
      IGNORE to deduplicate. That works for behavior_flags but not
      for cost — we'd pay for duplicate LLM calls. analysis_runs as
      a status table solves this at the trigger layer.

  - original: "Auto-trigger works on session end (non-blocking)"
    reframed: "Hook handler returns to caller before analysis starts; analysis runs in asyncio.create_task on the API server event loop. Hook latency overhead <10ms (measured); analysis dispatch may queue but does not block the hook return."
    delta: |
      Original is silent on what "non-blocking" means and against
      what. Three different things could be non-blocking: the hook
      handler return, the user's terminal exit, or the
      orchestrator's progress. Pinning "non-blocking from the hook
      handler's perspective" makes the latency budget testable
      (<10ms hook overhead) and clarifies that long analyses
      sharing the event loop with dashboard polling is a known
      tradeoff (dashboard responsiveness sub-metric covers it).
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "PydanticAI's current API requires async generators or streaming-only responses, incompatible with the frozen Awaitable[T] Protocol shape"
    rationale: |
      The Protocol in analysis/agent.py is the load-bearing
      abstraction. If PydanticAI cannot satisfy it without a wrapper
      layer >2 levels deep (hiding async generators behind asyncio
      tasks behind structured output adapters), the SDK choice is
      wrong. Better to drop PydanticAI for a thin direct-API agent
      (Anthropic SDK + Pydantic validation + manual tool dispatch)
      that satisfies the Protocol cleanly. The Protocol does not
      change; the SDK choice does. Verified during planning by a
      smoke test of PydanticAI's current Agent.run signature against
      the Protocol's analyze_segments signature.

  - condition: "LiteLLM cannot be configured to bypass fallback for validation errors (i.e., its retry policy fires on schema mismatch as well as transport errors)"
    rationale: |
      If validation errors trigger LiteLLM's fallback chain, every
      malformed LLM output multiplies spend by N (number of
      fallback models). Without explicit control over this, the
      router's cost contract is broken. If LiteLLM cannot be told
      "only fallback on these exception types," replace it with a
      hand-rolled fallback wrapper around PydanticAI's per-model
      Agent — explicit retry on Timeout/RateLimit/ProviderError, no
      retry on ValidationError. We lose LiteLLM's provider abstraction
      for the fallback path and accept that cost.

  - condition: "Adapters cannot reliably emit EventType.SESSION_END for non-Claude-Code agents within Phase 2"
    rationale: |
      If only Claude Code emits session_end and we ship the
      event-driven trigger keyed on that event type, users on
      Codex/OpenCode get analysis only via the timeout sweeper
      (default 30 min latency). That's acceptable as a known
      tradeoff per-adapter, but it must be documented and the
      user-visible UX (dashboard "no analysis yet, will run in
      ~30min" message) must not silently look identical to the
      "analysis failed" state. Kill = adapter doesn't emit
      session_end → trigger is timeout-only for that adapter, with
      explicit user-visible status; do not silently delay.

  - condition: "Project secrets denylist for read_project_file cannot be enforced at the path-resolution layer (e.g., glob matching is unreliable across OSes)"
    rationale: |
      The denylist is the single most important security control in
      this issue. If it cannot be enforced consistently across
      macOS/Linux/Windows path conventions, the tool ships disabled
      by default and is gated behind explicit per-project opt-in
      with a known-secrets warning at config time. Better to lose
      the tool than leak secrets silently.

  - condition: "asyncio.create_task on the API server event loop introduces measurable hook-handler latency degradation under load"
    rationale: |
      The non-blocking property is more important than the trigger
      mechanism. If task spawning itself adds >10ms p95 to hook
      handlers (because event loop scheduling under load with
      concurrent dashboard polls causes head-of-line blocking),
      move dispatch out of the hook context: hook only writes a
      "pending" marker to analysis_runs; the periodic sweeper
      promotes pending → running. We accept higher start latency
      (up to sweeper period) in exchange for hook-handler isolation.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Users with secrets in their project tree"
    cost: |
      First materially-leaky surface in SecondSight. read_project_file
      pipelines local file content into a third-party LLM. If the
      sandbox + denylist fails (path traversal, glob mismatch,
      symlink to /home/.aws/credentials), the leak is silent on
      the SecondSight side and shows up later as "model trained
      on customer secrets" or "secrets visible in provider logs."
      Mitigated by: (a) path resolution rejects any escape from
      the registered project root, (b) default-deny denylist
      blocks .env / *credentials* / *.pem / id_rsa* unless
      project config explicitly allows, (c) every rejected path
      logged at WARN with the reason. Damage window: from first
      session running on a project with secrets to first audit
      log review. Could be days. The North Star sub-metric
      "read_project_file_path_escape_attempts" alerts on first
      occurrence so the audit window is "next session" rather
      than "next quarterly review."

  - who: "LLM API cost budget"
    cost: |
      First issue that actually burns LLM tokens at user expense.
      Default model is Haiku (cheap), but per-segment fan-out
      means a 200-tool-call session generates ~5-10 segment LLM
      calls; cross-session aggregator runs ~7 LLM calls (one per
      flag_type). A buggy router (retries validation errors,
      retries 4xx, retries inside the orchestrator's already-
      retried call site) multiplies this by 3-7x silently.
      Mitigated by narrowing fallback to transport errors only and
      adding a "fallback_chain_success_rate" sub-metric whose
      corruption signature explicitly catches "fallback fires on
      every call" — the canary for primary-key failures
      masquerading as success.

  - who: "API server event loop / dashboard responsiveness"
    cost: |
      Non-blocking analysis runs in the same asyncio loop as hook
      handlers and dashboard polling. A long-running LLM call
      (60s+ on Sonnet, larger sessions) holds an event loop slot
      while doing await network I/O — the loop should multiplex
      fine, but in practice asyncio coroutines that hit
      synchronous tool methods (e.g., read_project_file's blocking
      file open) will starve other coroutines. First user-
      observable symptom: dashboard 5s polling missing its window
      during analysis. Mitigated by ensuring all tool methods are
      either truly async or wrapped in run_in_executor; verified
      by a stress test in implementation.

  - who: "GUR-104 / GUR-106 downstream"
    cost: |
      Both depend on this issue producing real flag/directive rows.
      If trigger reliability is bad (events dropped, sweeper missing
      sessions), GUR-106 dashboard shows "no data" indistinguishable
      from "feature broken." Mitigated by: (a) analysis_runs status
      table makes "no analysis yet" vs "analysis failed" vs
      "analysis pending" distinguishable in the dashboard,
      (b) GUR-104's directive lifecycle treats missing analyses as
      a known recoverable state, not a corruption.

  - who: "Cameron (GUR-103 implementer)"
    cost: |
      Constrained by the frozen Protocol in analysis/agent.py. If
      PydanticAI's natural API shape disagrees with the Protocol
      (likely fine: PydanticAI is async-native; Protocol is
      async-first), the implementer either fights the framework or
      lobbies for a Protocol change. The latter requires updating
      analysis/agent.py + every test that uses it + GUR-102's
      callers. The kill condition above acknowledges this and
      provides the exit (drop PydanticAI, not the Protocol).

  - who: "Future adapters (Codex, OpenCode)"
    cost: |
      Trigger relies on EventType.SESSION_END. Codex / OpenCode
      adapters that don't emit it fall back to timeout-only (30
      min default analysis latency). Acceptable for v1, but it's
      a real UX cost on those adapters until they catch up.
      Documented in the kill condition above so we don't silently
      paper over it.
```

## observable_done_state

Solved: A SecondSight server is running. A user finishes a Claude
Code session; within 60 seconds the dashboard shows
`analysis_runs.status='completed'` for that session, the
`behavior_flags` table has rows for that session, and the
`directives` table reflects the post-aggregation top-N. With
the primary `ANTHROPIC_API_KEY` revoked, the next session
completes via fallback (`gpt-4o-mini` etc.) — fallback events
visible in logs. `secondsight analyze <session_id>` re-runs an
analysis and is a no-op on already-completed sessions unless
`--force` is passed.

Not solved: Sessions end without producing analysis_runs rows; or
analysis_runs rows stay `pending` forever; or duplicate triggers
produce duplicate behavior_flags / duplicate aggregator runs (cost
leak); or `read_project_file` returns content from outside the
project root or from denylisted paths; or hook latency p95 climbs
above 10ms; or dashboard polling latency degrades during analysis
runs; or fallback fires on validation errors and silently retries
malformed prompts across the whole chain.

The observable difference is a SQL count + a log inspection: post
session-end, `SELECT status FROM analysis_runs WHERE session_id=?`
transitions through `pending → running → completed` (or `failed`
with a recorded error), and the count of WARN-level
`read_project_file path_escape` log lines stays at zero across
all dogfooding.
