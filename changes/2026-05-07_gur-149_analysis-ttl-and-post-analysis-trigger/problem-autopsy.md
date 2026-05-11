# Problem Autopsy: GUR-149 — analysis_ttl_days + post-analysis trigger

## original_statement

> P3A-11 (analysis_ttl_days half). Wire `analysis_ttl_days` (default 365 days per SD §3.10.1) through
> `RetentionConfig` and the cleanup pipeline. The TOML loader already accepts the field shape; the
> consumer side is missing.
>
> Post-analysis cleanup trigger. Hook into the analysis orchestrator's per-session post-completion
> event so raw_traces for analyzed sessions can be reaped earlier than the global TTL when the
> operator opts in. Currently no such event exists.
>
> Blocked on GUR-101 analysis orchestrator — specifically the missing post-analysis-completion
> event hook.
>
> [Latest wake comment, local-board, 2026-05-07T08:34:24Z] The task has no blockers, so it should
> start to take this ticket.

## reframed_statement

Two related but separable wires:

1. **Steady-state TTL on analyzed material.** `RetentionConfig` currently resolves only
   `raw_traces_ttl_days`; extend it to also resolve `analysis_ttl_days` with the same precedence
   chain, and add a purger that reaps `session_reports` and `behavior_flags` rows older than the
   resolved TTL. The TOML loader's hands-off shape already accepts the field; the missing piece is
   resolution + a destructive operator.

2. **Eager cleanup on a per-session basis.** Add a post-analysis callback to the orchestrator
   (single-subscriber, constructor-injected) that, when the operator opts in via
   `[retention].cleanup_after_analysis = true`, asks the existing `RawTracesPurger` to reap *one
   specific session_id* immediately after `analyze_session` reaches `summary_written`. The original
   "event hook" framing in the issue overstates the surface — the requirement is met by a callable,
   not an event bus.

The board's "no blockers" comment resolves an ambiguity: previous heartbeats waited for GUR-101 to
deliver an "event surface," but the board accepts that the post-analysis hook is part of *this*
ticket's scope rather than a GUR-101 prerequisite.

## translation_delta

```yaml
translation_delta:
  - original: "Hook into the analysis orchestrator's per-session post-completion event"
    reframed: "Add a constructor-injected callback to Orchestrator (single-subscriber)"
    delta: >
      "Event" implied a pub/sub bus; the actual requirement is one-call. Naming it a callback
      avoids over-building. If GUR-101 ever grows a real event bus, this callback becomes a
      subscriber — that's a refactor, not a redesign.

  - original: "the missing post-analysis-completion event hook"
    reframed: "the surface itself doesn't exist; we'll add the smallest viable surface (callback)"
    delta: >
      The wake delta from prior heartbeats reads this as a hard blocker on GUR-101 work. The
      board's comment plus codebase inspection (orchestrator.py:239-381) shows the hook is a
      ~3-line addition to analyze_session, well within this ticket's blast radius.

  - original: "raw_traces for analyzed sessions can be reaped earlier than the global TTL"
    reframed: "RawTracesPurger.purge() called with one ExpiredSession synthesized on the spot"
    delta: >
      Reuses the GUR-147 destructive primitive. The "earlier than TTL" framing is just a
      different enumeration: instead of `enumerate_expired_sessions`, the trigger constructs a
      single ExpiredSession from the just-completed session_id + its last_event_at.

  - original: "P3A-11 analysis_ttl_days enforcement"
    reframed: "TTL on session_reports + behavior_flags; analysis_runs are out of scope"
    delta: >
      The plan_v2.md row mentions "analysis_ttl_days" generically. "analysis_results" in this
      codebase is two tables (session_reports + behavior_flags). audit rows (analysis_runs)
      are intentionally excluded because they are diagnostic material, not user-facing analysis.
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "Operator demand telemetry shows zero overrides of analysis_ttl_days after 6 months in production"
    rationale: >
      365d default means most users never hit the boundary in practice. If no one overrides it and
      no one complains about analysis growth, the feature is paying carrying cost (config plumbing,
      purger surface, tests) without delivering observable value. Death by metric.

  - condition: "Compliance/privacy policy mandates indefinite retention of analyzed material"
    rationale: >
      If a downstream regulator or stakeholder requires `session_reports` to be append-only for
      audit, the entire purger surface becomes a liability. In that world, `analysis_ttl_days`
      defaults to None and the cleanup path is opt-in only.

  - condition: "GUR-101 grows a proper event bus and adopts publishers/subscribers"
    rationale: >
      Then this ticket's callback becomes redundant infrastructure. Migration: keep the cleanup
      logic, replace the constructor-injected callback with an event subscription. The cleanup
      side is durable; the wiring is replaceable.

  - condition: "Operators want analysis preserved beyond raw_traces deletion (current default)"
    rationale: >
      This IS the default. SD §3.10.1 sets analysis_ttl_days = 365 vs raw_traces_ttl_days = 90.
      So `cleanup_after_analysis` MUST default to false — opt-in only. If we ever default it
      to true, we contradict SD and silently delete operator-visible artifacts they expected
      to keep around.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Operators who enable cleanup_after_analysis=true"
    cost: >
      Lose the ability to re-inspect raw events for analyzed sessions. Mitigation: opt-in default
      false; CLI `--dry-run` shows what would be reaped before they commit.

  - who: "Future debugging sessions on analysis correctness"
    cost: >
      If an operator suspects an analysis is wrong but raw_traces are already eagerly cleaned,
      the round-trip needed to debug is broken. Mitigation: cleanup_after_analysis=true is an
      explicit opt-in; logs flag every eager purge.

  - who: "Future contributors maintaining the callback contract"
    cost: >
      Contract drift between Orchestrator's callback signature and whatever consumer registers.
      Mitigation: callback signature documented; scar report names this risk; type hints
      (Callable[[str], None]) constrain shape.

  - who: "Operators with a typo in their config key (analysis_ttl_day vs analysis_ttl_days)"
    cost: >
      Silently fall through to the 365d default, no error. Mitigation: this is the same
      semantic as raw_traces; both fall through quietly. Not worsening it. Could add unknown-key
      warning later — out of scope for v1.
```

## observable_done_state

A SecondSight install configured with `[retention].analysis_ttl_days = 30` and
`cleanup_after_analysis = true` will, after running for >30 days, show no `session_reports` rows
older than 30 days AND will see raw traces for any analyzed session disappear within one cleanup
tick of the analysis completing — verifiable by querying the DB plus the FS path
`{home}/projects/{project_id}/sessions/{session_id}/`. The "not solved" state is the current
state: both knobs accepted by config, neither enforced. The boundary metric (`analysis_retention_drift`
defined above) flips from "unbounded" to "<= configured TTL + 1 cleanup interval".
