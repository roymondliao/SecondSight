# Problem Autopsy: directive-lifecycle-hygiene

## original_statement

> Directive layer 有兩種 injection: 1. Convention inject  2. hit
> inject. 只有 Convention inject 會有 TTL，但這個 convention 要被
> 追蹤，需要從 analysis layer 在分析被注入後的 session 資料是否
> 有改善，如果某個 convention 確實有影響到 agent 的行為，且持續
> 改善，那這個 convention 可能會慢慢下降權重，而如果某個
> convention 在 analysis 分析結果出來發現還是沒有改善，就需要
> 1. 調整 convention 的說明  2. convention 為維持重要的權重 (
> 權重只用在系統端，不會真的進入到 system prompt 內)。
>
> （後續修正：Before/after analysis granularity 屬於 analysis
> 的部分，先不處理；改用 aggregator 再次偵測當 signal — Option
> (ii)）
>
> （Operator, 2026-05-21）

## reframed_statement

Convention lifecycle is **pedagogical**, not cache-management. A
working convention has *taught* its lesson — it should fade
because the agent has internalised the pattern, freeing system-
prompt budget for what hasn't been learned yet. A failing
convention needs either *louder reinforcement* (maintain weight)
or *better wording* (auto-rewrite the instruction text via LLM).
This inverts the usual cache pattern where "used = valuable =
keep". Weight is system-side metadata only; the agent always sees
the same `instruction` text — it does not see its own learning
score.

Because Q-C (rigorous before/after baseline) was deferred to a
future analysis-layer change, Change 2 uses **aggregator
re-detection of the same `identity_key`** as the proxy signal:
re-promoted → boost weight; not re-promoted for N consecutive
runs → decay. This is a coarser signal than statistical baseline
comparison but uses only existing pipeline state.

## translation_delta

```yaml
translation_delta:
  - original: "convention 確實有影響到 agent 的行為，且持續改善 → 慢慢下降權重"
    reframed: "successful teaching = pedagogically appropriate to fade"
    delta: >
      The counter-cache-intuitive direction (success → fade) is
      load-bearing. Recorded explicitly because future readers
      may otherwise "correct" this to the conventional cache
      semantics during implementation.

  - original: "convention 沒效 → 1. 調整 convention 說明  2. 維持重要的權重"
    reframed: "failing convention: rewrite instruction (auto via LLM) AND/OR maintain reinforcement"
    delta: >
      Two actions are listed in the original; they are not
      mutually exclusive. Both fire — rewrite AND maintain
      weight — when a convention is judged ineffective. The
      conjunction is intentional, not a typo.

  - original: "權重只用在系統端，不會真的進入到 system prompt 內"
    reframed: "weight is system-side metadata; agent sees only the instruction text, never the score"
    delta: >
      Prevents agent meta-reasoning about its own learning
      trajectory ("I see convention X has weight 0.3, so I can
      ignore it"). Architectural decision, not a UX preference.

  - original: "（Q-C deferred）"
    reframed: "use aggregator re-detection as proxy signal (Option ii)"
    delta: >
      Without Q-C, the weight-update mechanism would have no
      input signal — Q-A and Q-B would also collapse. Option
      (ii) substitutes a coarser proxy from existing aggregator
      output, keeping Q-A/Q-B alive at reduced precision.
```

## interrogation_record

### Q-A: weight ≈ 0 behaviour

Selected (β): weight < threshold → status transitions to
`OBSOLETE`. **No PATCH-based revival.** Revival happens
autonomously when the underlying pattern recurs and aggregator
re-promotes the same `identity_key` — at which point weight is
boosted and status returns to `ACTIVE`. This is a semantic shift
for `DirectiveStatus.OBSOLETE` from "permanently retired" to
"dormant, autonomously revivable".

### Q-B: convention revision

Selected (P): auto-revision via LLM call, UPSERT preserving
`identity_key`. Three guardrails were added during interrogation
because the unbounded form has a "revision churn loop" failure
mode:

```yaml
revision_guardrails:
  - max_revisions_per_identity_key: "e.g. 3 per 30 days; over-limit → mark as 'stalled'"
  - revision_history_archived: "old instruction texts kept, not silently overwritten"
  - llm_double_check_on_rewrite: "reuses Change 1 must-have #5; verifies rewrite is substantive, not paraphrase"
```

### Q-C: before/after granularity

Selected (X) initially (project-wide baseline), then deferred
entirely. Change 2 instead uses Option (ii) — aggregator
re-detection of identity_key. Trade-off accepted: coarser signal
in V1, refined when Q-C ships as a future analysis-layer change.

## kill_conditions

```yaml
kill_conditions:
  - condition: >
      Active conventions never approach capacity ceiling in observed
      production; weight + lifecycle dynamics never trigger anything.
    rationale: >
      If the loop never accumulates, the hygiene Change 2 ships is
      dead code. Likely indicates Change 1's North Star (BehaviorFlagType
      rate) is also low → agents are well-behaved before Change 1
      intervened. Acceptable steady state; ship can remain but
      complexity should be re-evaluated.

  - condition: >
      Auto-revision (must-have #W4) consistently produces worse
      instruction text than the original (LLM double-check rejects
      most rewrites, or stalled count climbs sharply).
    rationale: >
      The "trust LLM to rewrite system rules" capability is the
      most invasive use of LLM in SecondSight. If it doesn't earn
      its keep, the revision pipeline (#W4) should be removed
      while keeping the rest of Change 2 (#W1, W2, W3, W5).

  - condition: >
      OBSOLETE semantic shift breaks downstream consumers in ways
      not surfaced during the implementation-time audit.
    rationale: >
      The shift from "terminal" to "dormant" is a contract change.
      If callers downstream of directives layer materially break,
      consider reverting OBSOLETE to terminal and introducing a
      new DORMANT status instead.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "future maintainer (incl. operator)"
    cost: "OBSOLETE semantic shift creates latent bugs in code paths that assumed terminal; every grep hit is potential refactor work"
    silent: false
    surfacing_channel: "implementation-time audit (planning); production bugs if audit misses sites"

  - who: "LLM API spend (operator, or end-user via own keys)"
    cost: "auto-revision pipeline invokes LLM whenever convention judged ineffective; unbounded projects could surprise"
    silent: false
    mitigation: "Guardrail #1 (max revisions per identity_key per N sessions) caps it; planning to set N conservatively"

  - who: "GUR-106 dashboard authors"
    cost: "must surface weight, revision history, OBSOLETE/ACTIVE resurrection trails — non-trivial UX work"
    silent: false
    affected_phase: "GUR-106 implementation phase"

  - who: "operator (yuyu_liao)"
    cost: "framework stewardship continuation from Change 1; no additional cost beyond what Change 1 already imposed"
    silent: false
```

## observable_done_state

1. **Active convention count remains bounded per project** by the
   capacity ceiling; LWS eviction events appear in lifecycle audit
   log when ceiling is touched. Conventions that haven't been
   re-promoted by aggregator across N consecutive sessions
   transition to `OBSOLETE` autonomously (no human PATCH required).

2. **`OBSOLETE` is a two-way state.** Conventions that resurface
   (pattern recurs and aggregator re-promotes the same
   `identity_key`) transition back to `ACTIVE` with boosted weight,
   without human intervention. Audit log shows both directions of
   transition over observed time.

3. **Convention text evolves autonomously.** For conventions whose
   `source_flag_type` continues to fire flags but whose
   `identity_key` is not re-promoted, the instruction text is
   auto-rewritten via LLM. Revision history is queryable per
   `identity_key`; the LLM double-check log shows accepted vs
   rejected rewrites; stalled conventions (over max-revision cap)
   are surfaced for operator visibility (not action — visibility
   only).

## design_tempo_marker

This change pairs three answers that all chose the "simpler
version" over the "more precise version":

- (β) over (γ) for weight-zero behaviour (status transition over
  PATCH-revivable)
- (P) over (Q)/(R) for revision (auto over queued/human-approved)
- (X) → then deferred entirely; Option (ii) substituted (re-
  detection proxy over baseline analysis)

The cumulative effect is: **autonomy completeness prioritised over
measurement precision.** Ship the full loop with proxy signals,
then refine signal quality based on production observation. The
opposite tempo (build precise signals first, ship later) was
implicitly rejected. This is recorded as the change's design
tempo so that future iterations don't accidentally invert it.

## known_blindspots

- **Convention attribution confound (Q-C deferral cost):** When
  multiple conventions are active simultaneously, Option (ii)
  cannot attribute behavioural improvement (or its absence) to a
  specific convention. Aggregator re-detection is a per-convention
  signal, but the *causal effect* of any individual convention on
  agent behaviour is not separable from the others until Q-C
  ships.

- **Cold start under low convention count:** When a project has
  few conventions (< 5 active), weight distribution statistics
  are noise-dominated. North Star sub-metrics in this regime are
  not informative; fall back to Change 1's BehaviorFlagType rate.

- **Auto-revision text quality:** LLM-rewritten instructions might
  be syntactically different but semantically equivalent. LLM
  double-check guardrail aims to reject this, but residual cases
  (slightly different wording, identical meaning) may still slip
  through and consume revision budget without behavioural impact.
