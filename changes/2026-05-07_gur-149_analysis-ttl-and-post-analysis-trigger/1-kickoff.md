# Kickoff: GUR-149 — analysis_ttl_days enforcement + post-analysis cleanup trigger

## Problem Statement

The retention surface that GUR-147 (P3A) shipped only enforces `raw_traces_ttl_days`. Analyzed
artifacts (`session_reports`, `behavior_flags`) accumulate without bound — there is no TTL
enforcement against them, and there is no path for an operator to reap raw traces *eagerly* for
sessions that have already been analyzed (the common operator desire: "once analysis is done,
the raw events are dead weight"). SD §3.10 promises both behaviors; the consumer side does not exist.

This issue (GUR-107b carry-forward) wires (a) `analysis_ttl_days` through `RetentionConfig` and the
cleanup pipeline, and (b) a post-analysis hook that, when enabled, asks the cleanup pipeline to
reap raw traces for the just-analyzed session immediately.

## Evidence

- `src/secondsight/storage/retention.py:19-20` explicitly defers `analysis_ttl_days`: *"This module
  only resolves raw_traces_ttl_days for GUR-147 scope. analysis_ttl_days defers to GUR-107b."*
- `Orchestrator.analyze_session` (`src/secondsight/analysis/orchestrator.py:239-381`) reaches
  terminal stage `summary_written` without emitting any callback or event — confirming the original
  blocker described in the issue body. There is no observer pattern.
- SD §3.10.1 (`docs/system_design.md:608, 1399`): default `analysis_ttl_days = 365` — explicitly
  longer than raw_traces (90d) because analysis is small and re-deriving requires LLM tokens.
- GUR-147 ship-manifest carry_forward (`changes/2026-05-06_gur-107_phase3a-retention-observation-api/ship-manifest.yaml:159-`): GUR-107b explicitly named with this scope.

## Risk of Inaction

- **`session_reports` and `behavior_flags` grow unbounded.** Even at low session volume, a deployment
  running for months will accumulate analyses for sessions whose raw traces are already gone. The
  operator has no eviction lever short of hand-rolling SQL.
- **Operators cannot eagerly reclaim disk.** A team that wants the "store traces only long enough to
  analyze" pattern (privacy, disk pressure, compliance) cannot get it today — they must wait the
  full `raw_traces_ttl_days` (90d default) for any single session, even ones already summarized.
- **The retention story remains half-implemented.** The CLI, config, and SD all reference both
  knobs; users will configure `analysis_ttl_days` in their TOML, see no error, and silently get
  no enforcement. That is the exact silent-failure pattern the GUR-147 review chain rejected.

## Scope

### Must-Have (with death conditions)

- **`analysis_ttl_days` resolution through `RetentionConfig.load()`** — same precedence chain as
  `raw_traces_ttl_days` (per-project → global → builtin 365d).
  - Death condition: drop if SD §3.10.1 changes to a single retention knob covering both raw and
    analyzed data, OR if telemetry shows zero operators ever override the default after 6 months.

- **`AnalysisResultsPurger`** — enumerates+reaps `session_reports` rows + their `behavior_flags`
  rows whose attribution timestamp (`session_reports.created_at` or
  `analysis_runs.completed_at`, decision pending) is at or before `now - analysis_ttl_days`.
  - Death condition: drop if analyses become append-only-by-policy (e.g. compliance forbids deletion
    of analyzed material).

- **Post-analysis trigger** — a callback hook on `Orchestrator` (`on_analysis_complete:
  Callable[[str], None] | None`) invoked once after stage transitions to `summary_written`. The
  retention layer registers a callback that, when `[retention].cleanup_after_analysis = true`,
  immediately purges raw traces for that one session.
  - Death condition: drop if the analysis orchestrator starts emitting a real event bus (then this
    becomes a subscriber, not a constructor-injected callback).

### Nice-to-Have

- `secondsight cleanup --analysis-only` flag for operator-initiated analysis-only reaps.
- Structured log line attributing source: `"reaped because cleanup_after_analysis=true"` vs
  `"reaped because analysis_ttl_days exceeded"`.

### Explicitly Out of Scope

- `raw_traces_ttl_days` — shipped in GUR-147.
- Observation API endpoints — shipped in GUR-147.
- A general event bus / observer infrastructure — the callback is a single-subscriber hook, not
  a pub/sub system. If GUR-101 grows a real event bus later, this becomes a subscriber refactor.
- Cascading reap of `analysis_runs` audit rows — those are audit material; their TTL (if any) is
  a separate policy decision.
- Soft-delete / archival semantics — `analysis_ttl_days` means hard delete in v1.

## North Star

```yaml
metric:
  name: "analysis_retention_drift"
  definition: "max(now - session_reports.created_at) for any row not covered by an active TTL contract"
  current: "unbounded"
  target: "<= configured analysis_ttl_days + one cleanup interval"
  invalidation_condition: >
    Compliance/privacy policy reverses to require permanent retention of analysis material; in that
    case `analysis_ttl_days` must default to None and the entire purger surface becomes opt-in.
  corruption_signature: >
    Operator sets `analysis_ttl_days = 365` in config but `session_reports` count grows
    monotonically — implies the consumer-side resolution silently dropped the value (typo in
    config key? mis-cased section header?). Detection: emit a structured INFO line at cleanup
    invocation naming the resolved value AND its source attribution
    (`builtin_default` vs `global_config` vs `per_project_config`).

sub_metrics:
  - name: "post_analysis_trigger_lag"
    definition: "wall-time between stage='summary_written' and raw_traces purge for that session"
    current: "infinite (no trigger exists)"
    target: "<= 1 cleanup tick OR synchronous (call-stack) purge — TBD in planning"
    proxy_confidence: medium
    decoupling_detection: >
      Trigger fires but no FS/DB rows are removed for the named session_id — typically means
      session_id passed to callback does not match the session_id stored in events. Add an
      assertion: callback's session_id MUST equal the session whose stage just advanced.

  - name: "cleanup_orphan_rate"
    definition: "behavior_flags rows whose session_id has no matching session_reports row after a cleanup pass"
    current: "n/a"
    target: "0"
    proxy_confidence: high
    decoupling_detection: >
      If FK constraints are absent and the purger deletes session_reports without also deleting
      behavior_flags for the same session_id, orphan rows accumulate. Detection: post-cleanup
      query for `behavior_flags WHERE session_id NOT IN (SELECT session_id FROM session_reports)`.
```

## Stakeholders

- **Decision maker:** local-board (CEO) — already greenlit via `a0a92005` and the latest wake comment.
- **Impacted teams:** Operators of self-hosted SecondSight installations; developers running the
  analysis pipeline locally.
- **Damage recipients:**
  - Operators who set `cleanup_after_analysis = true` in dev: they lose the ability to manually
    re-inspect raw events post-analysis. Mitigation: feature is opt-in, defaults to false.
  - Future contributors who must keep the callback contract aligned with whatever event bus
    GUR-101 (or a successor) eventually grows. Mitigation: scar report names this drift risk.
