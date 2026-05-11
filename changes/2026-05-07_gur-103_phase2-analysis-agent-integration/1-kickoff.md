# Kickoff: gur-103-phase2-analysis-agent-integration

## Problem Statement

Phase 2 has a runnable orchestration pipeline (GUR-102:
`analyze_session` / `aggregate_project`) gated behind a typed
`AnalysisAgent` Protocol (`analysis/agent.py`). The Protocol has zero
implementations: every call into it raises `NotImplementedError` because
no class is bound to it yet. There is also no mechanism that turns a
session-end event into a call to `analyze_session(session_id)` — the
orchestrator is callable but unreached. GUR-103 fills both gaps: it
(a) ships a PydanticAI-backed implementation of the Protocol with
LiteLLM fallback routing, (b) wires `read_traces` /
`read_project_file` / `query_structured_store` /
`read_historical_flags` as the agent's tool surface, and (c) bridges
session-end signals (event-driven + timeout fallback + manual CLI) into
non-blocking orchestrator invocations.

## Evidence

- `src/secondsight/analysis/agent.py` defines the Protocol with three
  async methods (`analyze_segments`, `aggregate_flag_type`,
  `summarize_session`) and no concrete class — confirmed by grep:
  `class .*AnalysisAgent` returns only the Protocol declaration.
- `src/secondsight/sdk/` does not exist (the SD §3.x tree at line 97
  reserves it; no directory present in repo).
- `src/secondsight/cli/` has `init / serve / status / sync / cleanup /
  app` but **no `analyze.py`** — the CLI surface table at SD line 1486
  declares `secondsight analyze` but it is not wired.
- `src/secondsight/adapters/claude_code.py:63` already normalizes
  `SessionEnd` → `EventType.SESSION_END`. The event reaches the
  observation pipeline today; nothing downstream subscribes for
  analysis dispatch. That is the trigger gap P2-15 fills.
- SD §5.4 lists four `AnalysisTools` methods. None exist in the repo
  (`grep -rn "read_traces\|read_project_file\|query_structured_store\|
  read_historical_flags" src/` returns zero hits). The orchestrator's
  agent calls today have no tool inputs because the implementation is
  the stub.
- GUR-104 (directive lifecycle) and GUR-106 (dashboard) sit downstream
  of this — both consume rows the orchestrator writes only when
  triggered. Until GUR-103 closes the trigger gap, those features have
  zero production data to render.

## Risk of Inaction

- **Phase 2 is shipped-but-dead.** GUR-100/101/102 each have green
  tests, but no end-to-end signal because the agent Protocol is a
  stub. The orchestrator is a function nobody calls. Without GUR-103,
  Phase 2 is a code museum.
- **Phase 1→2→3 chain breaks at the analysis joint.** Memory note
  `project_phase1_to_3_chain.md` records this dependency; GUR-104 +
  GUR-106 sit blocked on real flag/directive rows that only appear
  when an agent actually runs.
- **Cost-leak surface stays unbounded.** With no implementation, the
  cost question is theoretical. Once an agent exists, every session
  fans out into 1 segment-LLM call per segment + 1 aggregator call
  per flag_type. Without a router with explicit fallback policy and
  a model-selection algorithm grounded in observation records, Phase
  3 dogfooding could trip a runaway cost incident on day one.
- **Read-side data leakage stays unmodeled.** `read_project_file`
  pipelines local FS content to a third-party LLM. Until GUR-103
  defines the path-sandboxing + secrets-denylist policy, the
  threat surface stays unspecified.

## Scope

### Must-Have (with death conditions)

- **`AnalysisTools` (P2-11) in `analysis/tools.py`** — Four methods
  per SD §5.4: `read_traces(session_id)`, `read_project_file(
  project_id, path)`, `query_structured_store(query)`,
  `read_historical_flags(project_id)`. **Path-sandboxed**:
  `read_project_file` resolves `path` relative to the project root
  registered in `project_config` and rejects any resolved path
  escaping the root (no symlink traversal, no `..`-leak), plus a
  default-deny secrets denylist (`.env`, `*credentials*`,
  `*.pem`, `id_rsa*`, configurable in project config). Death
  condition: if any of read_project_file's first 50 calls in
  dogfooding resolves outside the registered project root or returns
  the contents of a denylisted file, this tool ships disabled by
  default and is gated behind explicit per-project opt-in.

- **`sdk/agent.py` PydanticAI `AnalysisAgent` impl (P2-12)** — A
  concrete class binding the three frozen Protocol methods to
  PydanticAI agent loops with `AnalysisTools` registered as tools,
  using `SegmentAnalysis` / `AggregateOutput` / `SummaryOutput` as
  output types so PydanticAI's structured output handles validation.
  Death condition: if PydanticAI's current API requires async
  generators or streaming-only and cannot satisfy the `Awaitable[T]`
  return shape of the Protocol without a wrapper layer >2 levels
  deep, drop PydanticAI; ship a thin direct-API agent (Anthropic SDK
  + Pydantic validation) that satisfies the same Protocol.

- **`sdk/router.py` LLM Router via LiteLLM (P2-13)** — Wraps the
  primary model with a `fallback_models` chain from SD §5.7.2.
  Failure modes that trigger fallback: `Timeout`, `RateLimit`,
  `ProviderError`, `APIConnectionError`. Validation errors do NOT
  trigger fallback (those bubble to the orchestrator as
  `AnalysisAgentError`). Total fallback chain timeout budget:
  configurable, default 60s. Death condition: if fallback latency
  on primary failure exceeds 30s in normal operation (LiteLLM
  probing overhead), reduce the chain to one-shot retry on a single
  fallback model with no probe phase.

- **`sdk/model_selection.py` (P2-14)** — Given (a) project config
  override (`analysis.model`), (b) global config
  (`analysis.models.<agent_type>`), and (c) `agent_type` from the
  observation record (most-recent session's `agent_type` column when
  config = `auto`), return primary + fallback model spec for the
  router. Death condition: if `agent_type`-based inference produces
  a model the router cannot reach (e.g., user has Claude Code
  configured but no `ANTHROPIC_API_KEY`) more than 5% of the time
  in dogfooding, demote inference to "always use config default,
  ignore observation `agent_type`" and surface a one-time setup
  warning at session-end.

- **`sdk/trigger.py` + observation hook (P2-15)** — Three trigger
  paths:
  1. **Event-driven**: subscribe to `EventType.SESSION_END` in the
     observation pipeline; on receipt, spawn
     `analyze_session(session_id)` in a non-blocking task
     (`asyncio.create_task` on the API server's event loop). Hook
     handler returns to caller before analysis starts — measured
     hook latency overhead must stay <10ms.
  2. **Timeout fallback**: a periodic sweeper (every 60s) finds
     sessions with `last_event_ts < now() - timeout_minutes` and no
     `session_end` event and no `analysis_status='completed'` row;
     synthesizes a session-end and dispatches as above. Timeout
     default: 30 minutes (config: `analysis.session_timeout_minutes`).
  3. **Manual**: `secondsight analyze [SESSION_ID]` Typer subcommand
     that calls the orchestrator directly (foreground, blocking,
     prints progress). With no `SESSION_ID`: runs against the most
     recent un-analyzed session.

  Death condition: if event-driven trigger raises hook latency above
  100ms in any path (measured at p95 in dogfooding), the spawn moves
  out of the hook handler entirely (hook only writes a "analysis
  pending" marker; sweeper picks it up). The non-blocking property
  is more important than the latency between session-end and
  analysis-start.

- **Single source of truth for "session was analyzed"** —
  `intelligence.db` gains an `analysis_runs` row per session with
  status (`pending` / `running` / `completed` / `failed`). The
  trigger paths all check this table before dispatching to ensure
  exactly-once semantics across event/timeout/manual. Death
  condition: if duplicate `analyze_session(same_id)` calls produce
  duplicate behavior_flag rows (i.e. the orchestrator's
  `INSERT OR IGNORE` discipline doesn't hold under concurrent
  triggers), serialize via a per-session asyncio lock at the
  trigger layer, not the orchestrator.

### Nice-to-Have

- Concurrent per-segment LLM calls within a single
  `analyze_segments(prompts)` batch (sequential is fine for v1).
- A `secondsight analyze --watch` mode that tails the analysis
  status table.
- Streaming progress to stdout for `secondsight analyze` (a single
  final "done" log line is enough for v1).
- Pluggable fallback policy beyond LiteLLM's built-in (one
  hard-coded chain order is enough; SD §5.7.4 prescribes it).

### Explicitly Out of Scope

- **CLI mode agent (SD §5.7.3)** — borrowing the user's coding-agent
  binary as the analysis agent. SDK mode only this issue. CLI mode
  is a follow-up.
- **Directive lifecycle transitions** (active → effective / obsolete /
  re-activated) — GUR-104.
- **Dashboard rendering** (analysis status table, fallback events) —
  GUR-106.
- **Cross-adapter session-end normalization** — adapters already emit
  `EventType.SESSION_END`; if other adapters (Codex, OpenCode) don't
  emit it, that's a per-adapter issue, not GUR-103's scope.
- **Background process / daemon for the timeout sweeper** — runs
  inside the existing API server event loop; if the server is down,
  the timeout fallback is naturally inactive (manual CLI still works).
- **Per-call cost telemetry** — `logging.info(provider, model, tokens,
  duration_ms)` is enough; persisted cost rollups are a Phase 3+
  observability concern.

## North Star

```yaml
metric:
  name: "session_end_to_first_directive_latency_p95"
  definition: |
    For each session whose final event is session_end (event-driven
    or timeout-synthesized), the p95 wall-clock from session_end
    timestamp to (a) at least one BehaviorFlag row OR an explicit
    empty-segments report row written for that session, AND (b) the
    aggregate_project run that consumes that session's flags
    completing. Measured across the most recent 50 analyzed sessions.
  current: null  # no agent implementation today
  target: 120  # seconds
  invalidation_condition: |
    The metric is wrong if the dominant cost is LLM provider latency
    rather than SecondSight orchestration. If primary-model p95
    latency for a single segment call exceeds 60s in dogfooding, the
    end-to-end target is governed by the LLM, not us; switch to
    "orchestration overhead p95" (subtract LLM-call duration) as the
    metric this layer is responsible for.
  corruption_signature: |
    p95 stays under target while behavior_flag rows for new sessions
    drop to zero — detector is short-circuiting silently (catch-all
    except, JSON parse failure swallowed by router) and "completed"
    means "completed without producing output." Conversely, p95 stays
    under target while fallback router triggers fire on >50% of calls
    — primary model is silently broken (auth, rate limit) and we're
    paying for fallback every time.

sub_metrics:
  - name: "trigger_to_orchestrator_dispatch_latency_p95"
    current: null
    target: 5  # seconds
    proxy_confidence: high
    decoupling_detection: |
      Hook handler latency stays <10ms while dispatch latency climbs
      above 5s → asyncio.create_task is queueing behind a long-running
      task on the same event loop (e.g., a previous analysis). Detect
      via tagged trace ID linking session_end log to
      analyze_session_started log; alert when delta > 30s.

  - name: "fallback_chain_success_rate"
    current: null
    target: 0.95  # of calls that hit the chain (i.e., primary failed), fraction reaching success before exhausting chain
    proxy_confidence: medium
    decoupling_detection: |
      Total chain success vs. fallback chain success diverging:
      total stays high while fallback success drops means we've
      stopped trying fallbacks (router silently exited the chain).
      Verified by counting fallback-attempt log lines per primary
      failure; alert when ratio < 1.

  - name: "read_project_file_path_escape_attempts"
    current: null
    target: 0  # absolute, not rate
    proxy_confidence: high
    decoupling_detection: |
      A non-zero count is a security incident, not a quality metric.
      Logged at WARN whenever the sandboxer rejects a path; alert on
      the first occurrence in production.
```

## Stakeholders

- **Decision maker:** Project lead (board user) — locks the
  fallback chain default, the model-selection inference policy
  (`auto` vs. always-config-default), and the read_project_file
  default-deny denylist at the planning gate.
- **Impacted teams:**
  - GUR-104 (directive lifecycle) — consumes directive rows
    aggregator writes only when triggers fire reliably.
  - GUR-106 (dashboard) — needs to render `analysis_runs` status
    column for user-visible progress / failures.
  - Adapters (claude_code, future codex/opencode) — must emit
    `EventType.SESSION_END` on lifecycle end. Claude Code already
    does (verified at adapter line 63); other adapters TBD.
- **Damage recipients:**
  - **Users with secrets in their project tree** — `read_project_file`
    can leak `.env` / private keys to a third-party LLM unless the
    sandbox + denylist holds. First incident = data breach.
  - **LLM API cost budget** — first issue where Phase 2 actually
    burns tokens. A buggy retry/fallback (retrying validation
    errors as if they were transient) multiplies spend by N
    fallbacks per call.
  - **API server event loop** — non-blocking analysis runs in the
    same event loop as hook handlers and dashboard polling. A
    long-running analysis call could starve other requests if not
    isolated. First place that surfaces is dashboard polling
    latency during a session-end.
  - **Cameron / GUR-103 implementer** — locked into the Protocol
    GUR-102 froze. If the Protocol shape blocks PydanticAI's
    natural usage pattern, implementer either inherits the
    awkwardness or has to lobby for a Protocol change across two
    issues.
