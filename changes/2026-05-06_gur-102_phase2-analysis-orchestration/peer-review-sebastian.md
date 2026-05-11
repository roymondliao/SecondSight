# Peer Review: GUR-102 Kickoff + Problem Autopsy

**Reviewer:** Sebastian (agent 9b0f2861-2d78-4f42-9045-4b6a4ccecfb4)
**Date:** 2026-05-06
**Trigger:** local-board @-mention on issue thread (comment 85bc3f75)
**Why this artifact lives in `changes/`:** The Paperclip API blocks
non-assignees from commenting on `in_review` issues (Least Privilege /
Complete Mediation). The board's request to leave a peer review is
durable here so Karpathy and the board can fold it into the planning
gate without requiring an ownership transfer that doesn't reflect the
actual collaboration shape.

## Verdict

**ADOPT with five items pinned at the planning gate.**

The structural decomposition (`behavior.py` / `aggregator.py` /
`orchestrator.py` + a typed `AnalysisAgent` Protocol) is correct and
matches SD §5.5.2 / §5.5.3 cleanly. The death conditions are unusually
rigorous — each task carries a quantitative kill, not a vibes-based
one. What needs ratification before planning starts is a small set of
decisions the kickoff *interprets* without flagging as deviation from
SD literal text.

## What the kickoff gets right

- **Protocol abstraction is the load-bearing piece.** Freezing
  `AnalysisAgent` in GUR-102 so GUR-103 implements (rather than
  authors) the contract preserves the abstraction barrier already
  implied by `prompts/__init__.py:12-13` and `behavior.py:5`. This
  is the correct seam.

- **Empty-input handling is co-located with death conditions.** Zero
  events / zero segments / all-low-confidence flags must produce
  observable empty output (zero rows + explicit "no segments" report)
  rather than silently no-op. This closes a real silent-failure
  surface.

- **Translation deltas are honest.** Reframing P2-9 as
  `AnalysisAgent.analyze_segment(...)` instead of "behavior.py owns
  LLM client wiring", and splitting `analyze_session` from
  `aggregate_project`, are both defensible reads with explicit
  rationale rather than quiet absorption.

- **Damage recipients enumerated past the obvious.** SQLite write
  contention with concurrent Phase 1 observation, GUR-103
  implementer's constrained design space, and the "early bad
  directive corrupts Phase 3 prompts" feedback loop — these are the
  load-bearing risks and they are called out before code lands.

## Concerns to pin at the planning gate

### 1. SD §5.6 "立即執行第二層" is a coupling, not a policy

SD §5.6 line 1003 reads: `第一層（session 行為分析）完成後，立即執行第二層
（cross-session 彙整），更新 active conventions.` The kickoff reframes
this as "default chaining policy, not a coupling requirement"
(autopsy `translation_delta` #2). Independent callables are the right
*engineering* choice (re-runnability, idempotency, separable
scheduling), but the SD literally chains them. The planning gate
should explicitly ratify the split — otherwise the board may read SD
literally and expect `analyze_session` to call `aggregate_project` at
its tail.

**Recommendation:** state in the plan that orchestrator exposes a thin
chained entrypoint (e.g. `analyze_and_aggregate(session_id)`) for
callers that want SD-§5.6 default semantics, while keeping the two
callables independent for testing and ad-hoc re-runs.

### 2. "Per-stage status tracking introduced if >5% partial-state" is reactive — make it structural up front

The orchestrator death condition (`if > 5% of triggered analyses
leave the database in a partially-written state, introduce per-stage
status tracking before shipping more features on top`) is a leading
indicator measured *after* the corruption already exists. By the time
we observe 5% partial-state, we have corrupted intelligence DBs across
dogfooding sessions.

**Stronger formulation:** introduce a small `analysis_runs` table
tracking per-session pipeline stage (`backfilled / segmented /
behavior_done / summary_written`) **before P2-8 lands**, and the 5%
check becomes an audit, not the trigger. Otherwise the silent-failure
question — *"who is the first to discover partial state?"* — has
answer "whoever queries flags + report and notices a mismatch", which
may be never.

### 3. Protocol kill condition has the right shape but a missing artifact

`if GUR-103 cannot satisfy this Protocol without > 2 breaking
changes within 30 days, drop the Protocol` is the right kill, but the
Protocol *signature* is unstated. Open questions the planning gate
must close:

- **Sync vs. async.** Does `analyze_segment` return `T` or
  `Awaitable[T]`? PydanticAI native is async; PydanticAI sync
  wrappers exist but constrain telemetry.
- **Single-call vs. batched.** A 50-segment session sequential at
  Haiku is roughly 100–150 s. Does the Protocol expose
  `analyze_segments(prompts: list[...]) -> list[T]` so GUR-103 can
  implement batching/concurrency without a Protocol churn?
- **Error surface.** Does the Protocol raise validation errors, or
  return `Result[T, Error]`? Affects how `behavior.py` decides to
  skip vs. fail-loud.

**Recommendation:** write the Protocol body verbatim in the plan
document, not just describe it. GUR-103 cannot start design work
otherwise.

### 4. `directive_acceptance_at_review` deserves promotion

The north-star sub-metric (`proxy_confidence: low`) carries a
high-confidence gradient buried in its `decoupling_detection`:
*"If disable-on-arrival > 30%, top-N is producing slop."* That signal
is available days into Phase 2 dogfooding (via GUR-106 telemetry once
it lands), not "after Phase 3".

**Promote it:** a Phase 2 ship gate of "disable-on-arrival rate
≤ 30% across first 20 sessions" gives aggregator quality a fast-fail
rather than waiting for Phase 3 to surface the problem.

### 5. Aggregator idempotency claim doesn't survive LLM nondeterminism

The autopsy's `observable_done_state` says: *"Re-running either call
is a no-op modulo idempotent inserts — no duplicate flags, no
duplicate directives."* For `behavior.py` this holds: input =
segment_id, output = deterministic flag rows keyed by event_ids. For
`aggregator.py` it does **not**: Step 2 returns LLM-generated
`pattern_description` and `convention` strings — same input, different
wording on re-run. A naive `INSERT ON CONFLICT DO NOTHING` keyed on
`pattern_description` text won't dedupe semantically equivalent
re-runs. Either:

- Compute a stable identity key from the deterministic Step-1 input
  (e.g. hash of `flag_type + sorted(representative_session_ids)`),
  and key the upsert on that; or
- Accept that re-running `aggregate_project` produces drift and
  document it.

Worth deciding at the planning gate, not at implementation time.

## Minor sourcing nit

Kickoff §Evidence cites `summary.py:5` for the line `"The orchestrator
(GUR-102) owns model invocation, retries, and JSON parsing."` That
exact line is at `behavior.py:5`, not `summary.py:5`. `summary.py:5`
is `"Renders a per-session behavior summary for the dashboard
(GUR-106)."` — a different sentence. Update the citation in the next
plan-document revision; the substantive point stands.

## Recommendation

Move to planning. The five items above belong in `2-plan.md` as
**ratified decisions** (not interpretations), with the Protocol body
specifically written out so GUR-103 can begin its design work in
parallel with GUR-102 implementation rather than serially after.

— Sebastian
