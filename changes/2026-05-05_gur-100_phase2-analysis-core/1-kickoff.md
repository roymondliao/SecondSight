# Kickoff: GUR-100 — Phase 2 Analysis Core (Tables + Segmenter + Metrics)

## Problem Statement

Phase 1 ships a pipeline that observes coding-agent behavior and lands
events in `intelligence.db`. Nothing yet *interprets* those events.
Phase 2 begins with the data-layer foundation that every subsequent
analysis component depends on:

1. A **`behavior_flags` table** that LLM analysis (GUR-101) writes into.
2. A **`directives` table** that cross-session aggregation (GUR-101) and
   the lifecycle PATCH endpoint (GUR-104) read/write.
3. A **segmenter** that turns the flat `events` row stream into the
   per-user-prompt structure SD §5.3.1 expects as input to the
   per-segment LLM prompt.
4. A **supplementary-metrics** computer that produces the
   `total_tokens / unique_files / duration / error_count` summary the
   LLM uses as cheap context per SD §5.3.1 step 2.

Without GUR-100, GUR-101 (analysis prompts) cannot produce or persist a
single behavior flag, and GUR-104 (CLI + REST) has nothing to expose.
GUR-101 and GUR-106 are both blocked by GUR-104, which is blocked by
this issue. The whole Phase 2/3 chain pivots on these four pieces being
consistent with SD §5, §7, and the memory-pinned schema contracts.

## Evidence

- **`events` table is the only structured store today.** `src/secondsight/storage/events_table.py`
  + `events_repository.py` exist and are exercised by Phase 1 tests.
  No table holds analysis output — every analyzer would have nowhere to
  write.
- **`schemas/analysis.py` does not exist** (verified via `find src -name "analysis*"`
  — only `api/schemas.py`). SD §5.5.1 designates this file as single
  source of truth for `BehaviorFlagType`. GUR-100 P2-1 is the issue
  that must create it.
- **No segmenter in `src/secondsight/observation/` or `analysis/`.** Only
  `pipeline.py` and `tracker.py` exist; both are write-side components
  (event ingest), not read-side assembly. SessionTracker already stamps
  `segment_index` on each event at ingest, so the segmenter is a
  *grouping + tool-use-pair assembler*, not a re-segmenter.
- **`tool_use_start` / `tool_use_end` pairing is unsolved.** Phase 1
  persists both event types as separate rows with their own
  `sequence_number`. The analysis prompt in SD §5.5.2 expects a
  per-segment `events` array where each tool-use has a single rich
  shape (target, success, duration_ms). Pairing must happen somewhere;
  GUR-100 P2-3 is that somewhere.
- **Directive lifecycle contract is pinned in memory but not in code.**
  `project_directive_lifecycle_contract.md` mandates soft-disable +
  `disabled_at` + `disabled_reason` columns; SD §7.4 does not yet
  include those columns. GUR-100 must ship the table with these
  additions and patch SD in the same PR (same pattern as the
  BehaviorFlag `confidence` field).
- **Stale memory pointer.** `project_phase1_to_3_chain.md` claims "GUR-104
  owns directive table + schema". GUR-104's actual description (read
  via API) is CLI + REST endpoints only. The table location is GUR-100,
  not GUR-104 — surface this so the next reader is not misled.

## Risk of Inaction

If GUR-100 ships incomplete or with the wrong shape:

- **GUR-101 cannot start.** The analysis prompt has no schema to
  validate against and no table to write into.
- **GUR-104 + GUR-106 cascade-block.** The frontend dashboard's flag
  trends, directive list, and convention drill-down all need both
  tables. A two-week chain stalls on one schema.
- **Vocabulary drift becomes load-bearing.** If `behavior_flags.flag_type`
  is a free-text TEXT column with no app-level enum guard, the LLM can
  invent new flag types on the first hallucinated output. SD §5.5.1's
  "single source of truth" promise dies silently the first time a
  typo reaches the DB.
- **Soft-disable contract drifts.** If GUR-100 ships only `status TEXT`
  without `disabled_at` / `disabled_reason`, GUR-104 will need an
  ALTER TABLE to add them later — and SecondSight has no Alembic, so
  the migration story is "delete intelligence.db, lose history".
  The columns must land at table-creation time.

## Scope

### Must-Have (with death conditions)

- **MH-1 — `BehaviorFlagType` enum + Pydantic `BehaviorFlag` model in `analysis/schemas.py`.**
  Six enum values exact strings: `unnecessary_read`, `redundant_exploration`,
  `missed_shortcut`, `repeated_operation`, `wrong_tool_choice`,
  `excessive_context_gathering`. `BehaviorFlag` includes
  `confidence: Literal["high","medium","low"]` (memory contract,
  not yet in SD §5.5.2 — patch SD in same PR). Loaded by the analysis
  prompt builder (GUR-101) and the SQLAlchemy table validator.
  *Death condition:* drop when `BehaviorFlagType` is replaced by an
  open-vocabulary embedding (e.g. semantic clustering of free-text
  reasons replaces enums entirely). Until then, this enum is the
  contract anchor for GUR-101, GUR-104, and GUR-106.

- **MH-2 — `behavior_flags` SQLAlchemy table + repository.**
  Schema per SD §7.3: `id, project_id, session_id, segment_index,
  flag_type, event_ids (JSON-as-TEXT), intent_summary, reason, created_at`.
  Plus memory-mandated `confidence` column (TEXT, NOT NULL, app-level
  validated against `{high, medium, low}`). Repository methods:
  `insert(flag)`, `insert_many(flags)`, `get_session_flags(session_id)`,
  `get_project_flags_by_type(project_id, flag_type)`, `count_by_type(project_id)`.
  Idempotency: `INSERT … ON CONFLICT(id) DO NOTHING` — same contract
  as `events_repository`.
  *Death condition:* drop the table-and-repository when behavior flags
  move into a column on `events` itself (e.g. `events.flag_data JSON`)
  or when the analysis layer abandons SQLite for a vector store.

- **MH-3 — `directives` SQLAlchemy table + repository.**
  Schema per SD §7.4 + memory additions: `id, project_id, type, status
  (5-value enum), instruction, frequency, trigger_pattern, confidence,
  max_firing, source_flag_type, source_sessions, created_at, expires_at,
  updated_at, disabled_at, disabled_reason`. Repository methods:
  `insert(directive)`, `get_active_conventions(project_id)`,
  `update_status(directive_id, new_status, reason=None)` (sets
  `disabled_at` when transitioning to `disabled`; clears on
  re-`active`), `get_by_id(id)`. **The HTTP PATCH endpoint lives in
  GUR-104, not here** — GUR-100 ships only the in-process repository
  surface that GUR-104 will wrap.
  *Death condition:* drop when directives move to event-sourcing (i.e.
  every status change is an append-only row) instead of in-place
  status mutation. The repository is mutable-row-shaped today because
  the dashboard reads "current state" not "history".

- **MH-4 — `Segmenter` (analysis/segmenter.py).**
  Pure read-side assembly. Input: `session_id`. Output:
  `list[SegmentData]` where each segment has `user_prompt` (the
  triggering `user_prompt` event) + ordered `events` array with
  `tool_use_start`/`tool_use_end` paired into a single
  `ToolUseSpan { tool_name, target, success, duration_ms,
  start_seq, end_seq }`. Reads via `EventsRepository.get_session_events()`
  + groups by `segment_index`. **Unpaired `tool_use_start` (no matching
  `end`) MUST surface as a `ToolUseSpan` with `success=None` and
  `duration_ms=None` — never silently dropped.** Symmetric for
  orphaned `end` events (e.g. server crash mid-tool).
  *Death condition:* drop when SessionTracker emits already-paired
  logical tool-use rows directly into the events table (a single row
  per tool-use, with end-time backfilled on `tool_use_end`).

- **MH-5 — `analysis/metrics.py` `compute_segment_metrics(segment)`.**
  Per-segment supplementary metrics per SD §5.3.1 step 2:
  `total_tokens` (sum of `token_count` across thinking + response
  events; null tokens treated as 0 with a logged warning),
  `unique_files` (distinct `target` values across `tool_use_start`
  events with `tool_name in {Read, Edit, Write, …}`; tool-set is
  configurable), `duration` (last-event timestamp − first-event
  timestamp, in seconds), `error_count` (count of `tool_use_end`
  with `success=False`). Pure function over `SegmentData`, no DB.
  *Death condition:* drop when LLM is given raw events directly
  with no aggregate context (i.e. when context window costs are
  cheap enough that supplementary metrics no longer matter).

### Nice-to-Have

- **NH-1** — Span-splitting helper (SD §5.3.3: investigation /
  implementation / verification spans inside a long segment). Defer
  to GUR-101 — span splitting is an analysis-time concern; the
  segmenter has no need to produce spans pre-LLM.
- **NH-2** — `BehaviorFlagsRepository.delete_session_flags(session_id)` —
  re-analysis on a previously analyzed session would otherwise
  accumulate duplicate flags. Defer until the re-analysis CLI is
  designed in GUR-104; we don't know yet whether re-analysis should
  delete or append-with-version.
- **NH-3** — `DirectivesRepository.list_with_filter(status=..., type=...)` —
  needed by GUR-104's `GET /api/directives?status=active|disabled|all`.
  Add when GUR-104 starts; not needed for GUR-100's exit criteria.

### Explicitly Out of Scope

- **OoS-1** — HTTP/REST endpoints for directives or behavior flags.
  Lives in GUR-104. GUR-100 stops at the in-process repository surface.
- **OoS-2** — CLI subcommands (`secondsight analyze`, `secondsight directive`).
  Also GUR-104.
- **OoS-3** — LLM analysis itself (calling Anthropic/OpenAI, prompt
  rendering, parsing model output). GUR-101.
- **OoS-4** — Cross-session aggregation logic that turns flags into
  directives (semantic clustering, frequency thresholds). GUR-101.
- **OoS-5** — Alembic / migrations. The codebase has no Alembic;
  schema is `metadata.create_all(checkfirst=True)` on startup
  (per `project_phase1_to_3_chain.md`). Do not introduce Alembic in
  this issue.
- **OoS-6** — Dashboard UI changes. GUR-106.
- **OoS-7** — `read_project_file` analysis tool (SD §5.4). Not needed
  for any GUR-100 deliverable.

## North Star

```yaml
metric:
  name: "Phase 2 analysis-core data-layer correctness"
  definition: >
    Probability that, given a fully populated session in the events table,
    (a) the segmenter produces the exact segment+events structure SD §5.5.2's
    prompt expects, (b) supplementary metrics match a hand-computed reference
    on a fixture session, (c) writing & reading every behavior_flag and
    directive round-trips losslessly, and (d) lifecycle update transitions
    pass the soft-disable contract. Measured by green runs of the GUR-100
    test suite over 30 consecutive CI runs.
  current: unmeasured
  target: ">= 30/30 green (no flake budget — these are deterministic unit-shaped tests)"
  invalidation_condition: >
    If GUR-100 tests pass but GUR-101's first end-to-end run fails because
    the segmenter output doesn't match what the prompt expects, the test
    is wrong: it asserted on the segmenter's internal shape rather than
    the contract GUR-101 actually consumes. Mitigation: GUR-100 fixtures
    are co-authored with GUR-101's prompt-rendering test fixtures.
  corruption_signature: >
    "All tests pass, but the only fixture session is a 3-event happy path
     with no orphan tool_use_start, no null token_count, no concurrent
     sub-agent." Detected by spot-check: every must-have test must include
     at least one adversarial fixture (orphan event, null token, sub-agent
     nesting, status enum edge value).

sub_metrics:
  - name: "Segmenter robustness on adversarial event streams"
    current: unmeasured
    target: >
      100% of these scenarios produce a non-silent result:
      orphan tool_use_start, orphan tool_use_end, sub-agent nested events
      sharing a segment_index, empty segment (user_prompt with no following
      events), out-of-order sequence_number (should never happen but must
      surface, not silently re-sort).
    proxy_confidence: high
    decoupling_detection: >
      Proxy: synthetic fixture coverage. Main: real-world event streams.
      Decoupled when a real Claude Code session emits an event shape we
      didn't synthesize. Mitigation: replay one real fixture from
      tests/fixtures/claude_code/ through the segmenter as a smoke check.

  - name: "Schema-contract alignment with SD"
    current: 0% (SD §5.5.2 missing confidence field; SD §7.4 missing
            disabled_at, disabled_reason)
    target: "100% — SD patched in same PR as code; reviewer verifies diff includes both"
    proxy_confidence: high
    decoupling_detection: >
      Proxy: PR includes docs/system_design.md edits. Main: SD and code
      stay aligned over time. Decoupled when a future PR edits the table
      without touching SD. Mitigation: add a comment in
      analysis/schemas.py and behavior_flags_table.py pointing to the
      SD section, so the maintainer sees the link.

  - name: "Directive lifecycle status transitions"
    current: unmeasured
    target: >
      All transitions in the contract enumerated by tests:
      active → disabled (sets disabled_at + disabled_reason),
      disabled → active (clears both),
      active → superseded (analyzer-only path; user PATCH cannot reach
      this state — repository should accept it but PATCH endpoint in
      GUR-104 will reject it).
      Invalid transitions (e.g. expired → active without re-derivation)
      raise rather than silently passing.
    proxy_confidence: high
    decoupling_detection: >
      Proxy: enumerated transition tests. Main: production transitions
      stay consistent with contract. Decoupled when GUR-104 introduces
      a transition not anticipated here (e.g. disabled → superseded).
      Mitigation: scar-report any transition that arose during GUR-104
      and was not covered by GUR-100.
```

## Stakeholders

- **Decision maker:** yuyu_liao (project owner)
- **Impacted teams:**
  - **Karpathy (analysis-prompts agent, GUR-101 owner)** —
    consumes `BehaviorFlagType`, `BehaviorFlag` Pydantic model,
    `SegmentData`, supplementary metrics. Any shape change after this
    issue ships forces Karpathy to rewrite prompt-rendering code.
  - **Tianqi (me, GUR-104 owner)** — wraps the directives repository
    in HTTP/CLI. Repository method names + semantics are the contract.
  - **Frontend agent (GUR-106 owner, 7ff473d4)** — eventually queries
    directives via REST; the lifecycle status enum and the
    soft-disable semantics surface here are what they'll see.
- **Damage recipients:**
  - **Tianqi (me)** — owns long-term maintenance of two new tables, a
    repository per table, segmenter, metrics. ~600–900 lines of code +
    tests. If the schema is wrong, every analysis bug is a candidate
    for "is this the data layer or the LLM?"
  - **CI cycle time** — adds another schema-creation per
    `metadata.create_all` call; negligible for SQLite but the test
    suite grows by ~15–25 test files.
  - **`intelligence.db` users** — schema baked in at table-creation
    time. Without Alembic, a future column add means "delete the DB".
    Damage paid by anyone who has a non-trivial session history when
    the next schema change ships.

## Step 0 Commitments

1. **Most-wanted shortcut, rejected**: "ship `behavior_flags` and
   `directives` as bare TEXT columns with no app-level enum validation,
   defer enum enforcement to GUR-101's Pydantic model on the way in."
   Rejected — that creates two schemas (DB schema + Pydantic schema)
   that drift the moment one PR forgets to update the other. The DB
   table must enforce its own contract via `CHECK` constraints OR
   the repository must validate on insert. Decision in planning;
   neither option is "skip validation".
2. **This issue should NOT ship when**:
   - The SD §5.5.2 / §7.4 patches don't land in the same PR. Code +
     SD must be co-modified or one drifts immediately.
   - The segmenter's behavior on orphan `tool_use_start` is not
     enumerated by a test. Silent drop is the failure mode this
     issue most needs to prevent.
   - `disabled_at` / `disabled_reason` are absent from the table.
     Adding them later means losing the DB.
3. **Silent failure surface this issue closes**:
   - **Free-text flag_type drift** — LLM hallucinates a 7th flag type;
     repository writes it; SD §5.5.1's "single source of truth"
     promise silently dies. Closed by app-level enum validation in
     `BehaviorFlagsRepository.insert()` (DB CHECK preferred if
     SQLite supports it cleanly).
   - **Orphan tool-use silent drop** — segmenter encounters
     `tool_use_start` with no matching `end` (server crashed mid-tool)
     and silently omits it from the segment. LLM analyzes a segment
     that's missing a real action. Closed by emitting `ToolUseSpan`
     with `success=None`.
   - **Soft-disable forgotten** — directive disabled by GUR-104's
     PATCH endpoint, but `disabled_at` never set because column
     doesn't exist. Audit trail dies. Closed by shipping the
     columns at table-creation time.
   - **Status enum value drift** — directive.status set to a 6th
     value ("expired", "expired_old", whatever) by a future analyzer.
     Closed by app-level enum validation in
     `DirectivesRepository.update_status()` against the 5-value enum.
4. **What lives one year from now?**: MH-1 (the BehaviorFlagType enum)
   is the most load-bearing — every Phase 2 / Phase 3 component
   transitively depends on it, and changing it is a whole-system
   refactor. MH-2 (`behavior_flags` table) lives until the analysis
   layer abandons SQLite. MH-3 (`directives` table) lives until the
   feedback-loop architecture changes. MH-4 (segmenter) and MH-5
   (metrics) are the most replaceable — both are pure functions over
   the events table that GUR-101 could absorb if the boundary turns
   out to be wrong.
