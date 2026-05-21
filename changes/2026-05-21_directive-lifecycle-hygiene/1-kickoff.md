# Kickoff: directive-lifecycle-hygiene

## Problem Statement

`changes/2026-05-21_agent-native-hit-injection/` (Change 1) ships
the closed-loop hit injection itself but leaves the convention
side of the directive layer with two structural gaps:

1. **Auto-conventions are immortal.** Aggregator
   (`src/secondsight/analysis/aggregator.py:344-354`) creates new
   directives with `expires_at = None`. `lifecycle_automation`
   only enforces `expires_at` when set; without a PATCH (a manual
   human operation), auto-created conventions never expire. This
   violates invariant #1 (no human-in-loop) the moment
   self-evolution is the framework.

2. **No global capacity ceiling.** `top_n` in aggregator is a
   per-aggregation bound, not a per-project bound on total active
   conventions. Across many sessions, conventions accumulate without
   any policy that limits how many simultaneously occupy the
   system-prompt budget.

The fix is not "add `expires_at` setter to aggregator". A naïve
absolute-timestamp TTL is a set-and-forget time bomb that
penalises *all* conventions equally regardless of whether they
are still teaching anything. The redesign treats conventions as
**teaching material** rather than **cache entries**:

- **Successful conventions fade.** When a convention's pattern
  is being internalised (agent behaviour no longer triggers the
  matching flag_type), the convention's weight decays. Eventually
  it transitions to `OBSOLETE` and stops being injected.
- **Failing conventions persist or are revised.** When a
  convention's pattern keeps recurring despite injection (or
  hasn't reduced behavior_flags of its source flag_type), it
  either maintains its weight (try harder) or has its instruction
  text auto-rewritten by the LLM (try differently).

Weight is system-side only — never written into the system prompt
the agent reads. The agent always sees the same `instruction`
text; only the *system's* decision about whether and where to
inject it is weight-driven.

## Evidence

- `src/secondsight/analysis/aggregator.py:341-354` constructs
  `Directive` with explicit fields but no `expires_at` — confirmed
  immortal-by-default.
- `src/secondsight/feedback/lifecycle_automation.py:22` documents:
  "If `expires_at` is NULL for all conventions, expiry enforcement
  [doesn't apply]".
- `src/secondsight/api/directives.py:82` accepts `expires_at` only
  via PATCH — the only path that sets it is human action.
- The 3-tier TTL hierarchy already in place
  (`raw_traces_ttl_days` 90 / `analysis_ttl_days` 365 /
  `directive.expires_at` undefined) implicitly assumes user owns
  the directive's mortality. The shift to autonomous self-evolution
  invalidates that assumption.

## Risk of Inaction

- Convention count grows unbounded, eventually exceeding any
  reasonable system-prompt budget; either the system prompt
  exceeds model context, or some silent truncation drops
  conventions in an undefined order.
- The system claims to be a self-evolving loop while in fact only
  the *creation* half is autonomous; the *retirement* half still
  needs human action. Invariant #1 is not actually honoured at
  runtime.
- Operationally, the system-prompt budget pressure shows up as a
  partial / late-discovered failure mode that the Change 1 ship
  does not pre-empt.

## Scope

### Must-Have (with death conditions)

- **#W1 — Add `weight` to `Directive` schema**
  Float (likely `[0, 1]` or `[0, ∞)`; planning to lock). Defaults
  to high at creation. Aggregator sets initial weight when
  upserting new directives.
  **Death condition:** Convention scheduling becomes platform-native
  (Claude Code or equivalent ships convention-priority APIs) →
  weight becomes redundant → remove.

- **#W2 — Weight update on aggregator re-detection (Option (ii)
  signal)**
  Each session_end, aggregator runs. For each existing active
  convention, check whether its `identity_key` is re-promoted in
  this run.
  - If re-promoted → boost weight (pattern still active).
  - If not re-promoted across N consecutive runs → decay weight
    (pattern receding or already absorbed).
  No before/after baseline analysis (Q-C deferred to a future
  analysis-layer feature).
  **Death condition:** A more rigorous Q-C-style baseline analysis
  ships in a future change → this proxy signal is replaced;
  Change 2's wiring is removed in favour of the more accurate
  measurement.

- **#W3 — `OBSOLETE` semantic shift + transition automation**
  When weight falls below threshold → status transitions to
  `OBSOLETE`. `OBSOLETE` conventions are not injected into system
  prompts. If the underlying pattern recurs (aggregator
  re-promotes the same `identity_key`), the convention transitions
  back to `ACTIVE` with boosted weight. **No PATCH-based revival.**
  Audit of existing code that assumes `OBSOLETE = permanently
  dead` is required (see Implementation Hidden Cost below).
  **Death condition:** None at framework level — this is the new
  steady-state behaviour for the lifecycle.

- **#W4 — Auto-revision pipeline**
  When a convention's `source_flag_type` continues to generate
  behavior_flags but the convention's `identity_key` is not
  re-promoted (i.e. "the pattern is alive but this convention
  failed to catch it"), trigger an LLM call to rewrite the
  convention's `instruction` text. UPSERT preserves
  `identity_key` (same conceptual convention, new wording).
  Three guardrails apply:
  - **Max revisions per `identity_key` per N sessions** (e.g. 3
    per 30 days); over-limit → mark as `stalled` for operator
    awareness rather than continue churning.
  - **Revision history archived** (not overwriting old
    instructions in place); enables future "which revision
    worked best" analysis.
  - **LLM double-check on the rewritten text** (reuses must-have
    #5 from Change 1) to verify the rewrite is actually different
    and substantive, not a near-paraphrase.
  **Death condition:** Pattern-coverage is moved to a non-LLM
  mechanism (e.g. embedding-based search of conventions by
  flag_type), or platform ships native convention authoring →
  revision pipeline retired.

- **#W5 — Global active-convention capacity ceiling + LWS
  (least-weighted-shed) eviction**
  Per project, max N active conventions (config-tunable in
  `config.toml`). When threshold is exceeded, evict the
  lowest-weight active convention(s) — transition to `OBSOLETE`.
  Not LRU (least *recent*), but LWS (least *weighted*): the
  convention the system has least belief in.
  **Death condition:** Convention count proven sustainably below
  ceiling across all observed projects over M months → ceiling
  becomes nice-to-have → can be removed (but recommended to
  retain as backstop).

### Nice-to-Have

- Audit-log entries for every weight transition (boost / decay /
  revision / OBSOLETE / ACTIVE-resurrection) to enable
  post-mortem of self-evolution behaviour.
- Dashboard surfacing of `weight` alongside `frequency` so
  operators (via Q3 hard-stop #1 instrument) can sanity-check
  the system's belief about each convention.

### Explicitly Out of Scope

- **Before/after improvement analysis (Q-C).** Deferred. Change 2
  uses aggregator re-detection as proxy signal, not statistically
  rigorous baseline comparison. A future analysis-layer change
  will replace this proxy.
- **Convention TTL via absolute `expires_at`** for auto-created
  conventions. `expires_at` is preserved on schema and still
  honoured via the existing PATCH route as a *user override*;
  but auto-conventions do not set it.
- **`hit injection` lifecycle.** Hit injection has no persistence
  — per Change 1, it is a per-prompt transient meta-instruction.
  Only convention injection has lifecycle.

## North Star

```yaml
metric:
  name: "Convention lifecycle health"
  definition: >
    Composite observation: (a) active-convention count per project
    stable below capacity ceiling; (b) weight distribution shows
    healthy decay+reinforce dynamics (not stuck at uniform high);
    (c) OBSOLETE-to-ACTIVE resurrection rate non-zero (pattern
    re-emergence is being caught).
  current: not measurable yet (Change 2 not built)
  target: see definition
  invalidation_condition: >
    Operator-acknowledged metric is meaningful only if some
    auto-conventions are being created and at least some are
    being reinforced. If aggregator never produces conventions
    (project too simple), this metric is moot — fall back to
    Change 1's BehaviorFlagType rate as primary.
  corruption_signature: >
    (a) revision rate per convention climbs unboundedly → revision
        churn loop active; max-revisions cap should trip but
        verify it's actually stopping the loop.
    (b) weight distribution collapses to all-low (all conventions
        suspected dead) without OBSOLETE transitions firing →
        threshold misconfigured or transition automation broken.
    (c) conventions never reach OBSOLETE → boost dominates decay,
        loop accumulates without forgetting.

sub_metrics:
  - name: "Auto-revision rate (per convention per N sessions)"
    proxy_confidence: high
    decoupling_detection: >
      Track distribution shape of revision counts; if heavy-tailed
      (most never revised, a few revised 3+ times each) → healthy.
      If uniformly elevated → revision is firing on conventions
      it shouldn't.

  - name: "OBSOLETE → ACTIVE resurrection rate"
    proxy_confidence: medium
    decoupling_detection: >
      Resurrection signals the lifecycle isn't a one-way trapdoor.
      If 0 across observed period → either no recurring patterns
      (project signature), or threshold too tight (conventions
      never recover). Cross-reference with behavior_flag
      flag_type frequency over the same period.
```

## Stakeholders

- **Decision maker:** operator (yuyu_liao)
- **Impacted teams:** future analysis-layer authors (Q-C
  replacement); GUR-104 directive dashboard (must surface weight
  + revision history); existing code paths that reference
  `DirectiveStatus.OBSOLETE` (semantic shift audit).
- **Damage recipients:**
  - **future maintainer** — `OBSOLETE` semantic shift creates
    latent bugs in any code path that assumed `OBSOLETE = dead`.
    Hidden complexity beyond the visible scope.
  - **LLM API spend** — auto-revision pipeline calls LLM whenever
    a convention is judged ineffective; cost grows with project's
    behaviour-flag churn rate. Guardrail #1 (max revisions) caps
    it, but unbounded projects could surprise.
  - **operator** — same continuing cost as Change 1; framework
    stewardship.

## Chain Provenance

- **Inherits from:** `changes/2026-05-21_agent-native-hit-injection/`
  (Change 1). Specifically, Change 2 implements the lifecycle
  hygiene that Change 1 deferred via `forced_upgrade_trigger`.
- **Activation trigger:** active conventions per project
  sustained > N for M sessions (N, M defined at this change's
  planning). Until trigger fires, Change 2 ships ready but
  capacity ceiling is set sufficiently high to be inactive in
  early production.
- **Carryover invariants** (do not relitigate):
  1. Agent as human, human as agent.
  2. Rating + TTL: refined here to **weight + Option-ii signal**,
     not absolute timestamps.
  3. Analysis on session_end (already present).
  4. LLM double-check (reused for auto-revision verification).
  5. `config.toml` toggle (extends to capacity-ceiling N as
     tunable parameter).
  6. No subprocess; lifecycle work is in-process to existing
     analysis pipeline.
  7. Executability lens applies to Change 1's hit injection only;
     Change 2 operates entirely on the convention side.
  8. Convention capacity ceiling exists by design (this change
     gives it teeth).

## Implementation Hidden Cost (flagged at research stage)

The `OBSOLETE` semantic shift is not a localised schema change —
it requires grep-and-audit of every reference to
`DirectiveStatus.OBSOLETE` in the codebase (and any DDL /
dashboards / external consumers) to confirm whether each site
assumed "OBSOLETE = terminal" or already treated it as
"OBSOLETE = dormant". Sites that assumed terminal must be patched.
This audit task is planning-time work, but the cost is flagged
here so it does not surface as surprise scope during
implementation.
