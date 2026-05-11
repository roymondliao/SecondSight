# Peer Review Follow-up: GUR-102 Pre-thinking

**Reviewer:** Sebastian (agent 9b0f2861-2d78-4f42-9045-4b6a4ccecfb4)
**Date:** 2026-05-06
**Scope:** Gate-time input on `2-pre-thinking.md` before the board
resolves confirmation `1f6b885e`. Structural peer feedback only on
items where I have a non-obvious lens; G2/G3/G4 are board territory
and Karpathy's framing is sound.

## Verdict

**Pre-thinking is high-quality.** All five of my prior review items
are adopted with file/section anchors (A1, A4, B1, B3, C1–C3, E2,
F2). The four gaps and two uncertainties are real — not invented
caution — and each has a leaning interpretation rather than a stall.

I have substantive input on **one gap (G1) and two uncertainties
(U1, U2)**. The other items I reviewed are well-handled and I have
nothing to add.

## G1 — Use a dedicated `session_reports` table, not the folded form

Karpathy frames G1 as "fold into `analysis_runs.summary_json` vs.
dedicated `session_reports` table" and presents the trade-off as
"one fewer table vs. cleaner queries." The trade-off she did not
surface is **temporal identity coupling**:

- `analysis_runs` is keyed by `(session_id, started_at)` — its
  identity is the *pipeline run*. There are N rows per session
  across re-runs and failed-then-succeeded retries.
- A session report is keyed by `session_id` — its identity is the
  *session artifact*. There is logically one current report per
  session, regardless of how many pipeline runs produced it.

Re-runs (G3 with `force=True`) make this concrete. After three
re-analyses of the same session, the folded form has three rows
with `stage='summary_written'`, and "the current report for
session X" becomes:

```sql
SELECT summary_json FROM analysis_runs
WHERE session_id = ? AND stage = 'summary_written'
ORDER BY completed_at DESC LIMIT 1
```

That query lives forever in dashboard (GUR-106) code. Every
consumer that wants "the report" carries the pipeline-run filter
as incidental complexity. This is the silent-rot surface: the
abstraction *says* "session report" but the storage *says*
"latest of multiple pipeline runs that happened to land a
summary."

The cleanest decomposition matches the samsara axiom "if X
disappears, what feels pain?":

- If `analysis_runs` disappears, you lose pipeline-progress
  audit. Damage recipient: orchestrator's resumability story.
- If `session_reports` disappears, you lose the dashboard's
  primary content. Damage recipient: every GUR-106 view.

Different damage recipients = different tables. The
"one fewer migration" cost is paid once; the "incidental
pipeline-run filter on every report query" cost is paid forever.

**Recommendation:** dedicated `session_reports` table, schema
sketch:

```
id TEXT PRIMARY KEY            -- UUID
project_id TEXT NOT NULL
session_id TEXT NOT NULL UNIQUE
analysis_run_id TEXT NOT NULL  -- FK to analysis_runs.id (audit)
headline TEXT NOT NULL
key_findings TEXT NOT NULL     -- JSON array
body TEXT NOT NULL
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
INDEX (project_id, created_at DESC)
```

UNIQUE on `session_id` enforces "one current report per session".
On re-run, the orchestrator UPSERTs the row (same identity-key
pattern as B3 for directives — consistent across all "stable
artifact derived from LLM" surfaces in this issue). The
`analysis_run_id` FK preserves the audit trail without coupling
the artifact's identity to the run's identity.

This also resolves a second-order question the folded form
silently inherits: schema versioning of the SummaryOutput JSON.
A dedicated table can split structured columns
(`headline`, `key_findings`, `body`) so the dashboard queries
never need to parse JSON, and field-level migrations remain
ALTER TABLE, not JSON-shape migrations.

## U1 — Make the principle the load-bearing artifact

Karpathy lands on (b) verification over (a) defensive re-run.
That pick is right. The *reason* — and the thing worth pinning
explicitly in the plan — is the cross-issue contract principle:

> The orchestrator is a **consumer** of GUR-99's
> "events-are-persisted-by-session-end" contract, not a
> **recoverer** of it. If the contract is violated, the
> orchestrator fails loud at its entry stage; it does not
> silently re-execute upstream work.

Codified, this gives every future cross-issue boundary the same
default. GUR-103's session-end trigger consumes GUR-102's
"session has rows" contract; GUR-106's dashboard consumes
GUR-104's "directive lifecycle is consistent" contract. Each
boundary has a verifier, not a recoverer. The verifier has a
single job: "did upstream do its part?"

**Recommendation:** in `2-plan.md` §F.1 (or wherever F1 lands),
state the consumer-not-recoverer principle as a named decision,
and have the orchestrator's backfill stage call it out:
"Consumer of GUR-99 backfill contract; verifies, does not
recover." Future readers see the pattern, not just the local
choice.

## U2 — Cost analysis is conservative; short-circuit makes strict cadence cheaper

Karpathy's cost arithmetic on U2 (~$0.07/day/project on
aggregator alone, 10 sessions/day) assumes aggregator runs at
full fan-out per session. The chained wrapper
`analyze_and_aggregate(session_id)` can short-circuit:

```python
if behavior_flags_inserted_this_run == 0:
    return  # nothing new to aggregate; skip per-flag-type fan-out
```

Empty-segment sessions (which my prior review's empty-input
handling covers) and sessions where every segment yields zero
high-confidence flags both cost zero on aggregation. In
practice, sessions producing flags are a fraction of all
sessions. Real-world cost is probably 30–50% of the worst case.

This isn't a reason to relax strict cadence — Karpathy is right
to ship strict-for-v1 to match SD §5.6 literally. It's a reason
to add the short-circuit guard *now* in `analyze_and_aggregate`
so the cost trajectory in the ship-manifest is honest about the
floor, not just the ceiling.

**Recommendation:** add to D2 (or as new D4): "Aggregator
short-circuits when zero behavior flags were inserted in the
triggering session run; the chained wrapper checks the run's
flag-insertion count before invoking `aggregate_project`."

## Items I have nothing to add

- **G2 (config plumbing).** "Hard-code DEFAULT_CONVENTION_TOP_N
  with TODO" is the right scope-clean v1. AnalysisConfig now
  would be premature abstraction (one knob, no second consumer).
- **G3 (re-run semantics).** Karpathy's lean toward
  `force=True` is right — silent skip would hide data-loss
  bugs, raise without escape hatch is too brittle for ad-hoc
  re-analysis.
- **G4 (identity-key migration timing).** "No backfill needed,
  directives table is empty pre-Phase 3" is the simpler v1
  with no migration risk because there are no rows. Add the
  unique index in the same migration as the column.

## Where this artifact fits

This is gate-time input on confirmation `1f6b885e`. It is not a
veto — Karpathy's pre-thinking is sound and the plan can proceed
on her current dispositions. The G1 recommendation in particular
is structural enough that the board may want to ratify it as a
deviation from Karpathy's lean before plan-writing locks the
schema. U1 and U2 are framing/optimization tweaks that survive
plan-writing fine if folded later.

If the board accepts `1f6b885e` before reading this artifact,
the contents are still durable in-repo for `2-plan.md` revision.

— Sebastian
