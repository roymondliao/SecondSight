# Problem Autopsy: gur-104-phase2-analysis-cli-api

## original_statement

> Expose analysis results and directive management via CLI and API.
>
> **Tasks (P2-16 to P2-19):**
> - P2-16: CLI analyze subcommand — `secondsight analyze [--session ID]`: manual analysis trigger
> - P2-17: CLI directive subcommand — `secondsight directive --active --format json`: query active conventions
> - P2-18: Analysis API endpoints — `GET /api/analysis/summary`, `/sessions`, `/sessions/{id}`, `/sessions/{id}/flags`, `/trends`, `/aggregation`
> - P2-19: Directives API endpoints — `GET/PATCH /api/directives`
>
> **Exit criteria:**
> - `secondsight analyze` and `secondsight directive` work
> - All API endpoints return correct data
> - Dashboard can consume analysis API
>
> **Ref:** SD 9.2, 9.3, 10.4

## reframed_statement

GUR-104 is the **read- and control-surface** for the analysis state
that GUR-103 just made productive. Three audiences need to reach the
data: the dashboard (poll-driven UI), agents (CLI self-query),
operators (CLI + PATCH for directive lifecycle). The ticket text
also lists `secondsight analyze` (P2-16), but that already shipped
under GUR-103 P2-15 — so P2-16's net work is "decide whether to
re-do or accept GUR-103's implementation." Real net-new work:
1 CLI subcommand (`directive`), 6 read-only analysis endpoints, and
2 directive endpoints — gated by the directive lifecycle contract.

## translation_delta

```yaml
translation_delta:
  - original: "P2-16: CLI analyze subcommand — manual analysis trigger"
    reframed: "P2-16 is satisfied by GUR-103's existing cli/analyze.py — net-new work is zero unless an explicit delta surfaces"
    delta: |
      Ticket lists this as a task. Code shows it already shipped
      with full feature parity. Risk if not surfaced: future
      implementer re-implements and silently regresses GUR-103's
      server-mode + in-process trigger semantics. Resolution:
      explicit "out of scope, satisfied by GUR-103" in the plan.

  - original: "All API endpoints return correct data"
    reframed: "All API endpoints conform to api/observation.py's existing convention (frozen Pydantic, extra=forbid, required project_id Query, ETag from MAX(timestamp))"
    delta: |
      "Correct data" is underspecified. The risk is that GUR-104
      invents a parallel convention to observation.py — frozen vs
      mutable, optional vs required project_id, no ETag — and the
      dashboard sees a schizophrenic API. The reframe locks the
      convention to the existing precedent.

  - original: "Dashboard can consume analysis API"
    reframed: "The dashboard's intended polling rhythm (5s) and pagination model (cursor/ETag, single project) per dashboard_api_contracts memory must drive endpoint design"
    delta: |
      "Consume" doesn't tell us whether the dashboard polls or
      subscribes, paginates with cursor or offset, polls one
      project or all. Memory pins these. Without surfacing them,
      the plan defaults to whatever feels natural and breaks the
      dashboard's assumptions later.

  - original: "GET/PATCH /api/directives"
    reframed: "GET /api/directives lists active directives; PATCH /api/directives/{id} performs soft-disable / re-activate per directive_lifecycle_contract memory; no DELETE; idempotent on no-op"
    delta: |
      "PATCH /api/directives" is ambiguous between "PATCH the
      collection" (illegal REST) and "PATCH a single directive."
      Memory pins lifecycle: status enum transitions only, no
      DELETE. Reframe makes idempotency explicit — re-PATCHing
      the same status must not advance updated_at.

  - original: "secondsight directive --active --format json"
    reframed: |
      Same JSON shape as GET /api/directives — exactly one schema, used by
      both the server-mode and the no-server CLI path; once an agent reads
      it (Phase 3), the schema is sticky and changes break agents in the field
    delta: |
      The original phrasing implies the CLI invents its own
      output. If the CLI shape and the API shape diverge, an
      agent that works in dev (CLI) breaks in prod (API behind
      restart) — silently, by missing field. Reframe forces a
      single source-of-truth schema module.
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "GUR-105 (Phase 3 prompt injection) needs a directive shape that includes more than 'active conventions'"
    rationale: |
      The CLI's --active --format json output and GET
      /api/directives are intended to be read by the agent at
      prompt-injection time. If GUR-105 reveals it needs effective
      vs. obsolete vs. superseded directives, fields like
      `applies_to_segments` or `confidence_threshold`, the v1
      shape is wrong and we ship a contract we then break.
      Better to wait for GUR-105's shape requirements than to
      lock a shape that needs immediate v2.

  - condition: "Dashboard team (GUR-106) signals they don't poll the analysis surface — they subscribe to a SSE stream or read directly from a future event-bus"
    rationale: |
      The endpoint design (ETag, polling-friendly, paginated) is
      shaped by the assumption of REST-poll consumption. If
      GUR-106 architecture moved to push-driven, GUR-104's
      ETag and pagination work is wasted; ship a thin "fetch all"
      surface and put effort into the event channel instead.

  - condition: "PATCH /api/directives/{id} cannot reasonably take effect at runtime — Phase 3 cache invalidation is more than 2 issues away"
    rationale: |
      A PATCH that requires server restart to take effect is a
      worse UX than no PATCH at all (operators expect changes to
      stick on success). If GUR-105 pushes cache invalidation
      far out, downgrade PATCH to a CLI-only operation
      (`secondsight directive --disable ID`) that documents the
      restart requirement, and skip the HTTP endpoint until the
      cache exists.

  - condition: "All Phase 2 telemetry shows zero behavior_flags or directives in dogfooding for >2 weeks"
    rationale: |
      Read-side endpoints are valuable only if there are rows to
      read. If GUR-103 ships but the agent never produces flags
      (e.g., model selection consistently fails, sessions never
      reach session_end), GUR-104's endpoints are decorating
      empty tables. Better to debug Phase 2 production than to
      ship Phase 2 dashboards.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "GUR-106 (dashboard) implementer"
    cost: |
      Locked into the response shapes GUR-104 ships. A field
      added later requires a coordinated frontend+backend
      rebuild. A field removed later breaks the dashboard at
      next deploy. The cost is paid by whoever rebases.

  - who: "GUR-105 (Phase 3 directive injection) implementer"
    cost: |
      Locked into the directive JSON shape returned by both
      `secondsight directive --active --format json` and
      GET /api/directives. The shape is read by the agent at
      prompt-build time; once agents in the field read it, the
      shape is sticky. A future field rename is a breaking change
      for every running agent.

  - who: "Operator with a misfiring directive in production"
    cost: |
      If PATCH soft-disable doesn't invalidate Phase 3's
      prompt-injection cache (because that cache doesn't exist
      yet), the operator's PATCH succeeds but the directive
      keeps firing until server restart. The cost is the
      operator's time + every agent invocation between PATCH
      and restart that injects the disabled directive.

  - who: "API server event loop"
    cost: |
      Adds 8 new SQL-bound endpoints, all polled by the
      dashboard at 5s. A query plan regression in
      /api/analysis/aggregation or /trends could starve the
      observation endpoints (which are also polled). First
      manifestation: dashboard "lag" during periods of high
      flag write rate.

  - who: "Agent that calls `secondsight directive --active` from a no-server installation"
    cost: |
      If the no-server CLI path returns a shape that drifts
      from the API path (e.g., a field added later only to the
      API), the same agent works against a running server but
      fails against a no-server install. Failure is silent —
      the agent reads the field as missing/None and proceeds
      with degraded behavior. Cost is on whoever debugs why
      "the agent works on my machine but not in prod."

  - who: "Future implementer who re-reads the GUR-104 ticket"
    cost: |
      P2-16's overlap with GUR-103 is invisible from the
      ticket alone. Without the explicit "satisfied by
      GUR-103" callout in the plan, a future re-implementation
      could regress GUR-103's server-mode/in-process semantics
      or the 5-minute timeout in `_run_in_process_dispatch`.
```

## observable_done_state

After GUR-104 ships, an operator can run `secondsight directive
--active --format json` and pipe the output into an agent prompt;
they can run `curl http://127.0.0.1:8420/api/analysis/summary?project_id=X`
and see counts of analyzed sessions and flags by type; they can
PATCH a directive's status to `disabled` and the next GET excludes
it from the active list. Without GUR-104, none of these reach a
human or downstream agent — the operator's only access path is
opening SQLite. The observable difference is exactly the existence
of the read- and control-surface that GUR-103's writes feed into.
