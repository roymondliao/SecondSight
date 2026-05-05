# Problem Autopsy: GUR-100 — Phase 2 Analysis Core

## original_statement

> Build the DB structures and segmentation logic for the analysis layer.
>
> **Tasks (P2-1 to P2-4):**
> - P2-1: Behavior flags table — SQLAlchemy table + repository (INSERT,
>   query by session/project/flag_type)
> - P2-2: Directives table — SQLAlchemy table + repository (INSERT,
>   query active conventions, lifecycle update)
> - P2-3: Segmenter — `analysis/segmenter.py`: slice events from DB by
>   `segment_index`, pair `tool_use_start`/`end`
> - P2-4: Supplementary metrics — `analysis/metrics.py`: per-segment
>   `total_tokens`, `unique_files`, `duration`, `error_count`
>
> **Exit criteria:**
> - Both tables writable and queryable
> - Segmenter correctly groups events into segments
> - Metrics calculated per segment
>
> **Ref:** SD 5.3.1, 5.3.3, 7.3, 7.4

## reframed_statement

GUR-100 is the **data-layer foundation** for the analysis pipeline. It
does *not* perform analysis itself, *not* expose any HTTP/CLI surface,
*not* implement aggregation logic. It ships exactly four primitives:

1. The `BehaviorFlagType` enum + Pydantic model that every downstream
   consumer (analysis prompt, repository validator, REST schema) treats
   as the contract anchor.
2. Two SQLAlchemy Core tables (`behavior_flags`, `directives`) +
   their repositories, both following the same idempotency contract
   as `events_repository` (ON CONFLICT DO NOTHING on `id`,
   IntegrityError on contract violation).
3. A pure-function segmenter that reshapes the flat `events` row
   stream into the structured `SegmentData` that SD §5.5.2's
   per-segment LLM prompt expects, with explicit handling for orphan
   `tool_use_start`/`tool_use_end` (no silent drops).
4. A pure-function supplementary-metrics computer used by the LLM
   prompt builder as cheap context.

The work is "boring" in the sense that no prompt engineering, no LLM
calls, no HTTP. The risk is contract risk: every shape decided here
is consumed by 3+ downstream issues and is expensive to change after
ship.

## translation_delta

```yaml
translation_delta:
  - original: "Directives table — SQLAlchemy table + repository (INSERT,
               query active conventions, lifecycle update)"
    reframed: "Directives table + repository whose `update_status` method
               implements the soft-disable contract (status enum
               {active|disabled|expired|superseded|obsolete} +
               `disabled_at` + `disabled_reason`). HTTP exposure of
               `update_status` lives in GUR-104 (P2-19), NOT here."
    delta: "Wake context says 'lifecycle update' — ambiguous between
            'an in-process repository method' and 'a PATCH endpoint'.
            Memory contract `project_directive_lifecycle_contract.md`
            puts the PATCH endpoint in GUR-104. So GUR-100 ships the
            repository method only. This boundary is invisible in
            the original wording."

  - original: "Behavior flags table — SQLAlchemy table + repository
               (INSERT, query by session/project/flag_type)"
    reframed: "Behavior flags table per SD §7.3 with one addition not
               in SD: `confidence` column (Literal['high','medium','low']).
               Plus a Pydantic `BehaviorFlag` model in
               `analysis/schemas.py` that includes the same field.
               SD §5.5.2 must be patched in the same PR to add
               `confidence` to the prompt output schema. Repository
               validates `flag_type` against the enum on insert."
    delta: "Wake context omits the `confidence` field (memory contract
            `project_behaviorflag_schema_contract.md`) and omits the
            SD-update obligation. Without surfacing this, the SQL
            schema and the SD will silently drift on day 1."

  - original: "Segmenter — slice events from DB by segment_index,
               pair tool_use_start/end"
    reframed: "Segmenter is a pure-function read-side ASSEMBLER (events
               already have segment_index from SessionTracker). The
               load-bearing decision is how to handle orphan
               tool_use_start (no matching end) and orphan
               tool_use_end (no matching start). Default = emit
               ToolUseSpan with success=None and duration_ms=None;
               never silently drop."
    delta: "'pair tool_use_start/end' implies a happy-path pairing.
            The hard part is the unpaired case, which is where silent
            failure lives. The reframed spec makes the unpaired
            handling explicit."

  - original: "Supplementary metrics — per-segment total_tokens,
               unique_files, duration, error_count"
    reframed: "Pure function over SegmentData. total_tokens sums
               token_count across thinking + response events (NULL
               token_count → treated as 0 with a logged warning, NOT
               silently). unique_files counts distinct `target` values
               across configurable file-touching tool names
               (Read, Edit, Write, …). duration is last-event_ts minus
               first-event_ts in seconds. error_count counts
               tool_use_end with success=False."
    delta: "Original spec doesn't say what to do with NULL token_count
            (legitimate for some event types) or which tool names count
            as 'file-touching' for unique_files. Reframed makes both
            explicit and configurable."
```

### Resolved contradiction (memory vs. wake-context)

`project_phase1_to_3_chain.md` (line 11) says: *"GUR-104 — Phase 2
Analysis CLI + REST. … directive table + schema + PATCH /api/directives/{id}
lifecycle endpoint."*

GUR-104's actual issue description (fetched 2026-05-05): only CLI +
REST endpoints, no table.

GUR-100's actual issue description: *"Directives table — SQLAlchemy
table + repository (INSERT, query active conventions, lifecycle update)."*

**Resolution:** the table lives in GUR-100. The HTTP PATCH endpoint
lives in GUR-104. Memory was conflating "schema" with "endpoint" and
needs an update. (Action: revise `project_phase1_to_3_chain.md` post-
research.)

This resolved contradiction is recorded here so the next reader who
encounters the memory pointer doesn't re-derive the contradiction
from scratch.

## kill_conditions

```yaml
kill_conditions:
  - condition: >
      The analysis layer pivots away from per-segment LLM analysis
      (e.g. switches to whole-session embedding-based clustering with
      no segment boundary).
    rationale: >
      Segmenter and supplementary metrics become irrelevant. Tables
      survive — flag_type vocabulary still applies — but the segmenter
      and metrics modules can be deleted. We are NOT in this scenario:
      SD §5.3.1 explicitly mandates per-segment analysis as the v1
      design. Kill condition activates only if SD §5.3.1 is rewritten.

  - condition: >
      The decision to enforce flag_type as a closed enum is reversed
      (e.g. analyst wants to allow open-vocabulary tags from the LLM
      to cover edge cases).
    rationale: >
      MH-1 (the enum) and the repository's enum validation become
      worse-than-useless — they reject valid analyses. Probability:
      low for v1, but plausible at v3 once we have real data on
      whether the 6 flag types cover observed behavior. If killed,
      MH-2's CHECK constraint must come off the column.

  - condition: >
      Without Alembic, every schema change is a delete-the-DB event.
      If the lifecycle contract changes (e.g. new status value, new
      column for provenance), shipping under the current "no
      Alembic" rule means losing all history. Kill the work in this
      shape if the project decides Alembic must precede Phase 2.
    rationale: >
      The right move would be to introduce Alembic FIRST, then build
      Phase 2 tables under proper migration. Memory pin
      `project_phase1_to_3_chain.md` says the project decided the
      opposite ("Migration is the wrong term until Alembic is
      introduced"). If that decision flips, GUR-100 becomes wrong.
      Surface this before planning so the decision is explicit.

  - condition: >
      GUR-100 is split into multiple issues (one per table, one for
      segmenter, one for metrics) because the testing and review
      surface is too large for a single PR.
    rationale: >
      The four pieces are weakly coupled (segmenter doesn't know about
      directives, metrics don't know about behavior_flags). Splitting
      is a reasonable refactor of the issue itself if the resulting
      PR exceeds ~1000 LoC + tests. Surface this option in planning
      so it's a deliberate choice, not a default.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Tianqi (me) — backend engineer, GUR-100 + GUR-104 owner"
    cost: >
      Long-term maintenance of two SQLAlchemy tables, two repositories,
      a segmenter, a metrics module. ~600–900 lines of production code
      plus a test suite of similar size. Every analysis bug becomes a
      "data layer or LLM?" triage problem. Every DB schema change
      means losing intelligence.db (no Alembic).

  - who: "Karpathy (analysis-prompts agent, GUR-101 owner)"
    cost: >
      Hard contract dependency on `BehaviorFlagType` enum + Pydantic
      `BehaviorFlag` model + `SegmentData` shape. Any ergonomic flaw
      (e.g. flag_type values that are awkward in prompts, segment
      shape that requires Karpathy to flatten before rendering) is
      paid by GUR-101's prompt code, not by GUR-100. GUR-100 is
      committing GUR-101 to a contract sight-unseen.

  - who: "GUR-104 (CLI + REST, future me)"
    cost: >
      Repository methods are the API contract. If GUR-100 ships
      `update_status(directive_id, new_status, reason=None)` but
      GUR-104 wants to validate at the HTTP layer that
      `new_status` is one of the user-allowed subset
      ({active, disabled}), then GUR-104 must implement a duplicate
      enum guard. If GUR-100 ships an HTTP-aware enum guard at the
      repository layer, GUR-100 is leaking HTTP concerns into the
      data layer. Decision needed in planning.

  - who: "Frontend (GUR-106, 7ff473d4)"
    cost: >
      Eventually serializes BehaviorFlag and Directive to JSON. If
      GUR-100 ships `event_ids` as a JSON-encoded TEXT column, the
      REST layer (GUR-104) must JSON-decode-then-re-encode on every
      read. Worse if `confidence` is stored as TEXT but the dashboard
      expects a numeric. Surface format choices early so frontend
      doesn't get a string-vs-number surprise.

  - who: "intelligence.db on every developer's laptop (existing data)"
    cost: >
      Every Phase-1 user with collected events loses the DB at the
      moment GUR-100 ships, because metadata.create_all(checkfirst=True)
      will create new tables but cannot evolve existing ones. If we
      have any pilot users, communicate the loss before the merge.
      (As of 2026-05-05: probably zero pilot users; verify.)
```

## observable_done_state

When GUR-100 is solved: a fresh checkout + `secondsight init` + ingest
of one fixture session produces an `intelligence.db` with all three
tables (`events`, `behavior_flags`, `directives`); a Python script
that calls `Segmenter(events_repo).segment_session(session_id)`
returns a `list[SegmentData]` with paired tool-use spans (including
orphan-marked spans for adversarial fixtures); calling
`compute_segment_metrics(segment)` on each returns a dict with the
four keys; and `BehaviorFlagsRepository.insert(BehaviorFlag(...))` /
`DirectivesRepository.insert(Directive(...))` round-trip via
`get_session_flags` / `get_active_conventions` losslessly. All test
suites for these modules run green deterministically (no flaky
fixtures, no time-dependent assertions). SD §5.5.2 and §7.4 are
patched in the same PR.

When NOT solved: any of the above is missing, the segmenter silently
drops orphan tool-uses, the directive table lacks `disabled_at` /
`disabled_reason`, or the BehaviorFlagType enum drifts from the
six-value vocabulary in SD §5.5.1.
