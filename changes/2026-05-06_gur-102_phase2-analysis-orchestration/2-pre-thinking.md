# Planning Pre-thinking — GUR-102 Phase 2 Analysis Orchestration

Surfaces the information assumptions and gaps before writing `2-plan.md`.
Includes Sebastian's five peer-review items (peer-review-sebastian.md) so
the planning gate ratifies them as decisions, not interpretations.

## To write this plan, I am assuming

### A. Architecture (from research + accepted kickoff)

- **A1.** Three new modules + one new table: `analysis/orchestrator.py`,
  `analysis/behavior.py`, `analysis/aggregator.py`, plus a small
  `analysis_runs` table for per-stage tracking (Sebastian item 2,
  promoting from "introduce on 5% partial-state alarm" to "introduce
  up front").

- **A2.** `AnalysisAgent` Protocol is a typed seam between GUR-102 and
  GUR-103. GUR-102 freezes the contract; GUR-103 implements it on
  PydanticAI. Behavior detector and aggregator depend on the Protocol,
  not the implementation.

- **A3.** Dependency-injection friendly: orchestrator constructor
  receives `(events_repo, behavior_flags_repo, directives_repo,
  analysis_runs_repo, agent: AnalysisAgent)`. No global state, no
  module-level config reads. Tests provide a `FakeAnalysisAgent` that
  returns canned `SegmentAnalysis` / `AggregateOutput` / `SummaryOutput`.

- **A4.** Two callable entrypoints on the orchestrator —
  `analyze_session(session_id)` and `aggregate_project(project_id)` —
  plus a thin chained convenience `analyze_and_aggregate(session_id)`
  that composes the two for SD §5.6 default semantics (Sebastian item 1).

### B. Data model

- **B1.** `analysis_runs` table schema (new). One row per
  `(session_id, started_at)` orchestrator run. Tracks progress via
  enumerated stage transitions; serves as the source of truth for "did
  this session complete cleanly?" Schema sketch:
  - `id TEXT PRIMARY KEY` — UUID per run
  - `project_id TEXT NOT NULL`
  - `session_id TEXT NOT NULL`
  - `stage TEXT NOT NULL` — one of `pending | segmented |
    behavior_done | summary_written | aggregated | failed`
  - `started_at DATETIME NOT NULL`
  - `updated_at DATETIME NOT NULL`
  - `completed_at DATETIME` — nullable until terminal stage
  - `error_message TEXT` — nullable; populated when `stage='failed'`
  - `summary_json TEXT` — nullable; the SummaryOutput JSON blob
    (dual-use: tracks pipeline AND persists the session report)
  - INDEX (`project_id`, `session_id`)
  - INDEX (`project_id`, `stage`)

- **B2.** Session report persistence: SD §6 line 624 says "Analysis
  results (DB + filesystem JSON), 365 days". SD §7 does **not** define
  a `session_reports` table. GUR-102 introduces a **dedicated
  `session_reports` table** (Sebastian follow-up review — different
  damage recipients argument; see "Sebastian's follow-up — disposition"
  below). Schema:
  - `id TEXT PRIMARY KEY` — UUID
  - `project_id TEXT NOT NULL`
  - `session_id TEXT NOT NULL UNIQUE` — one current report per session
  - `analysis_run_id TEXT NOT NULL` — FK-style ref to `analysis_runs.id`
    (audit trail without identity coupling)
  - `headline TEXT NOT NULL`
  - `key_findings TEXT NOT NULL` — JSON array
  - `body TEXT NOT NULL`
  - `created_at DATETIME NOT NULL`
  - `updated_at DATETIME NOT NULL`
  - INDEX (`project_id`, `created_at DESC`)

  On re-run (G3 with `force=True`), the orchestrator UPSERTs by
  `session_id` UNIQUE — same identity-key pattern as B3 for directives,
  consistent across all "stable artifact derived from LLM" surfaces in
  this issue. `analysis_run_id` updates to the new run; the artifact
  identity stays bound to `session_id`. Filesystem JSON backup at
  `{home}/projects/{project_id}/sessions/{session_id}/session_report.json`
  per SD §7.2 line 209. DB and filesystem writes happen in the same
  orchestrator stage (`summary_written`).

  **Why dedicated, not folded into `analysis_runs.summary_json`:**
  pipeline-run identity (N rows per session across re-runs/retries) is
  not artifact identity (one current report per session). Folding
  pushes an incidental "WHERE stage='summary_written' ORDER BY
  completed_at DESC LIMIT 1" filter into every GUR-106 dashboard
  query forever. Structured columns also make field-level migrations
  ALTER TABLE rather than JSON-shape migrations.

- **B3.** Directives stable-identity hash (Sebastian item 5). The
  `directives` table has no identity-hash column. Plan adds one
  (`identity_key TEXT`) plus a UNIQUE index on
  `(project_id, identity_key)`. Hash input:
  `sha256(flag_type + "|" + sorted(representative_session_ids).join(","))`.
  The hash is computed in the aggregator before insert; `INSERT ON
  CONFLICT(project_id, identity_key) DO UPDATE SET instruction=?,
  updated_at=?` makes re-runs converge instead of drift. Migration is
  additive — existing rows get a backfilled hash on first read.

### C. AnalysisAgent Protocol contract (Sebastian item 3)

Pinned signature for the planning gate:

```python
class AnalysisAgent(Protocol):
    """Contract GUR-102 freezes; GUR-103 implements on PydanticAI.

    Async-first because PydanticAI is async-native. Sync callers wrap
    via asyncio.run at the Phase 1 hook boundary (GUR-103 task P2-15).
    """

    async def analyze_segments(
        self,
        prompts: Sequence[str],
        output_schema: type[T],
    ) -> list[T]: ...
    # Batched form. GUR-103 may run sequentially (default) or implement
    # concurrent batching. Single-segment calls use len(prompts) == 1.
    # Returns one validated output per input prompt; len(out) == len(in).
    # Raises ValidationError or AnalysisAgentError on irrecoverable
    # failure — caller (behavior.py / aggregator.py) decides skip vs.
    # fail-loud.

    async def aggregate_flag_type(
        self,
        prompt: str,
    ) -> AggregateOutput: ...
    # Single-call form. Aggregator does its own per-flag-type fan-out.

    async def summarize_session(
        self,
        prompt: str,
    ) -> SummaryOutput: ...
    # Single-call form. Orchestrator calls once per session.
```

- **C1.** Async returns. `Awaitable[T]`-based, not sync.
- **C2.** Batched form for segments to avoid Protocol churn when GUR-103
  adds concurrency. Aggregator and summary stay single-call (per-flag-
  type fan-out is aggregator policy, not Protocol concern).
- **C3.** Raises on irrecoverable failure. Returns `T` validated.
  Caller decides skip-segment vs. fail-pipeline. `AnalysisAgentError`
  is a new exception type co-located with the Protocol.

### D. Aggregator policy (SD §5.5.3)

- **D1.** Step 1 grouping: by `flag_type` only (SD §5.5.3 Step 1).
  No cross-flag-type clustering in v1.
- **D2.** Step 2: one LLM call per non-empty group. Empty groups skip
  the call entirely (no waste).
- **D3.** Step 3: merge all `AggregatePattern` instances across all
  flag_types, sort by `occurrence_count DESC`, take top
  `convention_top_n` (default `15`, SD §11.x line 1392). For v1 the
  default is hard-coded as a module-level constant
  `DEFAULT_CONVENTION_TOP_N = 15` with a TODO referencing future
  `analysis_config.convention_top_n`. Adding a config plumbing layer
  is out-of-scope here; GUR-104 or GUR-106 owns the config surface.
- **D4.** **Aggregator short-circuit guard** (Sebastian follow-up
  review). The chained wrapper `analyze_and_aggregate(session_id)`
  inspects the triggering run's `behavior_flags_inserted` count
  before invoking `aggregate_project`. If zero new flags landed,
  the wrapper returns without invoking the aggregator at all
  (no per-flag-type fan-out, zero LLM cost). This makes empty-
  segment sessions and all-low-confidence sessions cost nothing
  on aggregation. Strict-cadence with zero-flag-skip is the
  literal SD §5.6 reading; the short-circuit is on a no-new-input
  axis, not a debounce.

### E. Validation gates (Sebastian item 4)

- **E1.** Phase 2 ship gate: orchestrator + behavior detector + aggregator
  produce non-empty rows on a recorded test session. `pytest
  tests/analysis/ -v` is 100% green deterministically.
- **E2.** **Promoted from Phase 3:** disable-on-arrival rate. Once
  GUR-106 dashboard ships, "directives marked `disabled` within 24h
  of `created_at`" must be ≤ 30% across the first 20 sessions.
  Documented as a deferred validation gate in the ship-manifest, not
  blocking GUR-102 ship — but tied as a blocker for GUR-104 directive-
  lifecycle work to begin.

### F. Idempotency and re-runs

- **F1.** `analyze_session(session_id)` is re-runnable. On entry, if
  the latest `analysis_runs` row for this session is `stage IN
  (segmented, behavior_done)`, the run resumes from the next stage. If
  the latest row is `stage='failed'` or no row exists, a new run row
  is created. Successful prior runs (`stage='summary_written'`) are
  treated as "already done" — caller can pass `force=True` to re-run.
- **F2.** `aggregate_project(project_id)` is re-runnable via the
  identity-key UPSERT (B3). Re-runs converge: same Step-1 input →
  same `identity_key` → UPDATE existing row's `instruction` text. The
  LLM-generated text drifts but the row identity is stable.

## Gaps I cannot resolve from Research

### G1. ~~Session report DB persistence (B2) — schema gap in SD~~ — RESOLVED

SD §6 promises "DB + filesystem JSON" for analysis results, but SD §7
has no `session_reports` table. **Resolved at gate-time by Sebastian's
follow-up review:** dedicated `session_reports` table, not folded into
`analysis_runs.summary_json`. Reasons (now pinned in B2):
- Different damage recipients (samsara axiom): `analysis_runs`
  disappearance hurts pipeline audit; `session_reports` disappearance
  hurts every dashboard view. Different consumers = different tables.
- Temporal identity decoupling: pipeline-run identity (N rows per
  session) is not session-artifact identity (one current report per
  session). Folding pushes an incidental run-filter into every
  GUR-106 query forever.
- Schema-versioning ergonomics: structured columns make field-level
  migrations ALTER TABLE; JSON blob makes them shape-migrations.

Schema sketch in B2 above. Confirmation card on this gate is being
re-issued against the revised pre-thinking; the prior confirmation
(`1f6b885e`) is superseded.

### G2. `convention_top_n` config plumbing (D3)

SD §11 line 1392 sets `convention_top_n = 15` in a config example.
RetentionConfig (GUR-147) is the only config consumer in the codebase
right now; there is no `analysis_config` module. **Question for board:**
acceptable to hard-code `DEFAULT_CONVENTION_TOP_N = 15` in v1 (with a
TODO for future config plumbing)? Or should GUR-102 also introduce
`AnalysisConfig` alongside `RetentionConfig`?
- Hard-code: 1 line, scope-clean, defers a config-surface decision until
  there are 2+ config knobs to plumb together.
- AnalysisConfig now: matches RetentionConfig pattern, but adds ~150
  lines of plumbing for one knob. Premature abstraction risk.

### G3. Re-run semantics for completed sessions (F1)

Should `analyze_session(session_id)` on a session that already has
`stage='summary_written'` (a) skip silently, (b) raise
`SessionAlreadyAnalyzedError`, or (c) require explicit `force=True`?
**Question for board:** I lean toward (c) — silent skip would hide
data-loss bugs ("I called this 100 times and only one row exists"
should not be a happy path). Confirm or override.

### G4. Directives identity-key migration timing (B3)

Adding `identity_key` to `directives` table is a schema change. Two
sub-questions:
- Backfill strategy on first read of an existing row without the key:
  compute and write back lazily, or run a one-shot backfill in
  `create_schema()`? Lazy is safer (no startup cost on a large table)
  but means the unique index can only be added after backfill completes.
- For v1 (no rows yet in production), is it acceptable to add the
  column NOT NULL with no default and no backfill (simplest), since
  the directives table is empty pre-Phase 3? **Question for board:**
  accept "no backfill needed because no rows yet" as v1, or design for
  the post-deployment migration shape now?

## Uncertainties

### U1. Backfill stage — RESOLVED as **consumer-not-recoverer principle**

Resolution adopted from Sebastian's follow-up review: orchestrator's
backfill stage is a **verifier**, not a **recoverer**. Stated as a
named cross-issue contract principle (not a one-off local choice):

> The orchestrator is a **consumer** of GUR-99's
> "events-are-persisted-by-session-end" contract, not a
> **recoverer** of it. If the contract is violated, the
> orchestrator fails loud at its entry stage; it does not
> silently re-execute upstream work.

This generalizes: GUR-103's session-end trigger consumes GUR-102's
"session has rows" contract; GUR-106 dashboard consumes GUR-104's
"directive lifecycle is consistent" contract. Each cross-issue
boundary has a verifier with a single job — "did upstream do its
part?" — and fails loud rather than silently compensating.

Code shape: orchestrator's `_verify_session_complete(session_id)`
checks (a) `events.session_id = ?` count > 0 and (b) the session has
a recorded end-event (or session-end fallback marker). Missing → raise
`SessionIncompleteError`, surfacing the upstream contract violation
without obscuring it.

### U2. Aggregator cadence — RESOLVED as **strict + zero-flag short-circuit**

Resolution: ship strict-per-session-end cadence (matches literal SD
§5.6 reading). Cost arithmetic in earlier draft was the worst-case
ceiling; Sebastian's follow-up review pointed out the floor is
substantially lower because empty-segment sessions and all-low-
confidence sessions can short-circuit before any per-flag-type LLM
fan-out. Pinned in **D4** above: `analyze_and_aggregate(session_id)`
checks `behavior_flags_inserted` from the triggering run; if zero,
returns without invoking aggregator. This is a no-new-input skip,
not a debounce — strict cadence is preserved on the cadence axis.

Ship-manifest will report both numbers (worst-case ceiling at
~$0.07/day/project at 10 sessions/day with all sessions producing
flags; floor is ~zero on empty/no-flag sessions) so future cadence
optimization is data-driven rather than guess-driven.

## Sebastian's review items — disposition mapping

| Round 1 item | Disposition | Where ratified |
|------|------------|----------------|
| 1. Chained `analyze_and_aggregate` wrapper | **Adopt** | A4 above |
| 2. `analysis_runs` table up front | **Adopt** | A1 + B1 above |
| 3. Protocol body verbatim | **Adopt** | C1–C3 above |
| 4. disable-on-arrival ≤ 30% promoted to ship gate | **Adopt** (deferred validation) | E2 above |
| 5. Aggregator stable-identity hash | **Adopt** | B3 + F2 above |
| Citation nit (`summary.py:5` → `prompts/behavior.py:5`) | **Note in plan §Evidence** | 2-plan.md will cite correctly |

## Sebastian's follow-up review — disposition (gate-time input)

| Round 2 item | Disposition | Where ratified |
|------|------------|----------------|
| G1 — dedicated `session_reports` table (override fold) | **Adopt** | B2 (revised) |
| U1 — consumer-not-recoverer named principle | **Adopt** | U1 (resolved) |
| U2 — short-circuit guard on zero new flags | **Adopt** | D4 (added) |
| G2/G3/G4 — agree with my disposition | **No change** | G2/G3/G4 unchanged |

## Output state (post-follow-up)

- Gaps: **3 unresolved** (G2 hard-code default; G3 force=True;
  G4 no-backfill). G1 resolved by Sebastian's follow-up.
- Uncertainties: **0** (U1, U2 both resolved by Sebastian's follow-up).
- All 5 Sebastian round-1 items folded; all 3 actionable round-2 items
  folded.

**Confirmation `1f6b885e` superseded.** A new confirmation against the
revised pre-thinking revision is being created in the same heartbeat.
The human gate accepts (or overrides) the revised disposition before
plan content is written.
