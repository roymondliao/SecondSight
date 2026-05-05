# Planning Pre-thinking — Information Assumptions

> Required gate before writing `2-plan.md` per samsara `planning` skill.
> Identifies what I am about to assume to write the plan, and where my
> assumptions diverge from what Research established. Each gap requires
> a decision before Step 2 begins.

## To write this plan, I am assuming

- **A1 — Test layer location:** new e2e tests will live in
  `tests/integration/test_phase1_e2e.py`. (Kickoff stated this; existing
  conventions allow either `tests/integration/` or extending
  `tests/scripts/test_hook_fallback.py`.)
- **A2 — Server fixture reuse:** the existing `real_secondsight_server`
  fixture in `tests/scripts/conftest.py` will be lifted to a shared
  fixture (e.g. moved to `tests/conftest.py` or imported from
  `tests/scripts/conftest`). Confirmed by reading the fixture: it
  binds port 0, creates a tmp `SECONDSIGHT_HOME`, registers a
  `_ClaudeCodeAdapterStub`, and yields `{port, home, project_id, session_id}`.
- **A3 — Idempotency proof for Path A + B:** confirmed by reading
  `storage/filesystem_backfill.py` — `EventsRepository.insert` uses
  `INSERT … ON CONFLICT DO NOTHING` keyed on `event.id`. Re-running
  sync is structurally idempotent.
- **A4 — Test prerequisites:** the e2e suite requires `bash`, `curl`,
  `jq`, and (optionally) `shellcheck` available on `PATH`. Tests will
  skip with a loud message naming the missing tool — never silently
  green-pass.

## Gaps I cannot resolve from Research

These are decisions the Research/kickoff did not — and could not —
constrain, because they depend on factual ground truth I learned only
by reading code. **Do NOT proceed to Step 2 until these are decided.**

### G1 — MH-3 scope: fallback recovery into DB does not exist in Phase 1 [BLOCKING]

**What the issue's exit criterion says:**

> "Backfill mechanism restores DB from fallback"

**What my kickoff assumed:** that `secondsight sync` re-INSERTs events
from `fallback_events.jsonl` into the DB.

**What the code actually does**
(`src/secondsight/storage/filesystem_backfill.py:16-22`):

> Path C — fallback_events.jsonl replay (server-down) is **deferred to
> a follow-up**. The fallback envelope shape currently lacks event_id /
> sequence_number, so replaying it would require reconstructing those
> from the adapter pipeline. Phase 1 sync ARCHIVES the fallback file
> (atomic move to a timestamped .bak) … See P1-13 scar carry-forward.

**Implication:** the GUR-99 exit criterion as literally written cannot
be tested green against Phase 1 code. There are three viable scopes,
each shaping the entire plan differently:

- **Option G1-α — Test what exists.** MH-3 becomes:
  *"Server down → hook writes fallback_events.jsonl → server starts
  → `secondsight sync` archives the file to a timestamped .bak →
  re-running `sync` is idempotent (no double-archive)."*
  Replay-into-DB stays explicitly out of scope for GUR-99 and is
  marked as the Phase 1 → Phase 2 carry-forward (already noted in
  the source as "P1-13 scar carry-forward").
  **Cheapest. Most honest about what Phase 1 actually delivers.**

- **Option G1-β — Implement Path C, then test it.** GUR-99 grows to
  include the missing fallback replay. Adds ~1 implementation task
  (Path C in `filesystem_backfill.py`) before the e2e test. This
  changes the GUR-99 surface from "test gate" to "feature + test".
  Risk: the deferral comment names a real reason — fallback envelope
  lacks `event_id`/`sequence_number` — meaning Path C requires the
  adapter pipeline to invent IDs from the bash envelope shape, which
  has knock-on correctness implications (collision risk on retry).

- **Option G1-γ — Carve a new ticket for Path C, defer MH-3.** Open
  a child issue (e.g. GUR-99a "P1-13 fallback replay") and have GUR-99
  ship MH-1, MH-2, MH-4, MH-5 only. MH-3 reduces to the archive
  assertion (same as G1-α's scope). The child issue blocks the
  full Phase 1 → Phase 2 transition until resolved.

**Recommendation: G1-α.** The deferral is documented in code and a
scar item already tracks Path C. Re-opening that scope under GUR-99
expands the issue beyond "integration test" and tangles it with a
correctness question that needs its own research (envelope ID
reconstruction). G1-α tests the actual Phase 1 contract honestly.
G1-γ is acceptable if the board wants the carry-forward to have its
own thread; functionally equivalent to G1-α for GUR-99 itself.

### G2 — MH-2 sequence does not exercise segment_index increment [BLOCKING for MH-2 spec]

**What the kickoff said:** fire `start → pre/post-tool-use × 2 → end`,
assert `segment_index` increments per SD §3.9 rules.

**What the code does**
(`src/secondsight/observation/tracker.py:193-195`):

> `if partial.event_type == EventType.USER_PROMPT: state.segment_index += 1`

**Only `USER_PROMPT` increments `segment_index`.** Pre/post-tool-use
do not. The kickoff's six-event sequence would yield identical
`segment_index = 0` on every row — the assertion would pass for the
wrong reason (no increment to test).

**Decision:** rewrite MH-2's sequence to include at least two
`USER_PROMPT` events:

```
session-start
  → user-prompt   (segment_index → 1)
    → pre-tool-use, post-tool-use  (still 1)
  → user-prompt   (segment_index → 2)
    → pre-tool-use, post-tool-use  (still 2)
session-end
```

Acceptance asserts segment_index transitions at the two USER_PROMPT
events and remains stable for the bracketed tool-use events. **This
is a correction, not a scope change.** Surfaced for visibility.

### G3 — Sub-agent nesting uses explicit start/end events, not a payload field [BLOCKING for MH-2 spec]

**What the kickoff said:** "sub-agent nesting (when
`payload.parent_agent_id` is set)".

**What the code does**
(`src/secondsight/observation/tracker.py:200-237`): nesting is driven
by `EventType.SUB_AGENT_START` / `SUB_AGENT_END` events, with
`data["sub_agent_id"]` carrying the id being pushed/popped.
Stack mismatch (unmatched end, empty stack pop) raises
`SubAgentStackMismatch` — which is a death case worth testing.

**Decision:** rewrite MH-2 to also fire `sub_agent_start` /
`sub_agent_end` pairs, asserting:
- `depth` increases when start arrives, decreases on matching end
- `sub_agent_id` reflects stack top
- mismatched end is rejected at the API layer (HTTP 4xx, not silent)

Adds **one death-path scenario** to acceptance.yaml: "sub_agent_end
on empty stack must surface as HTTP error, never silently advance."

### G4 — MH-4 latency artifact directory has no precedent [non-blocking, decide-or-document]

**What the kickoff said:** record latency histogram to
`tests/_artifacts/gur99_latency.json`.

**What the code shows:** no `tests/_artifacts/` directory exists; no
test currently writes JSON artifacts. Closest precedent: tests write
fixtures as `tests/fixtures/*` but those are read-only.

**Decision options:**
- **G4-α** — create `tests/_artifacts/` and add it to `.gitignore`
  (ephemeral, regenerated each run, useful for local dev only).
- **G4-β** — print histogram to stderr and let CI capture as job
  output; no on-disk artifact (simplest, but trend analysis requires
  external CI history).
- **G4-γ** — write to `pytest --junit-xml`-adjacent `reports/` dir
  used by `2026-05-04_phase4-1-ci-pipeline` (need to check if that
  plan establishes a convention).

**Recommendation: G4-β** unless Phase 4 CI plan already establishes
a convention. Trend analysis is nice-to-have; printing-to-stderr is
the minimum-viable form.

### G5 — Test file location convention [non-blocking, decide-or-document]

**Two viable choices:**
- **G5-α** — extend `tests/scripts/test_hook_fallback.py` with new
  classes `TestPhase1E2EIntegration*`. Reuses fixtures by import,
  zero new conftest needed.
- **G5-β** — new `tests/integration/test_phase1_e2e.py`. Cleaner
  separation, but duplicates fixture imports.

**Recommendation: G5-β** with shared fixtures lifted from
`tests/scripts/conftest.py` to `tests/conftest.py`. The kickoff
already named "tests/integration/" implicitly; consistency wins.

## Uncertainties

None remaining once G1–G5 are decided. The code-reading pass resolved
all "I cannot tell if Research intended X or Y" questions.

## Output state

- **Status:** `gaps exist — STOP, do not proceed to Step 2`.
- **Gate type:** human (board) confirmation required, with explicit
  decisions on G1 (BLOCKING), G2 (correction), G3 (correction), G4
  (preference), G5 (preference).
- **What I will NOT do without confirmation:** write `2-plan.md`,
  `acceptance.yaml`, `tasks/`, or any test code. The plan structure
  depends materially on G1's resolution.
- **What I propose:** present this artifact via `request_confirmation`
  with a recommendation summary; on accept I'll write the plan
  reflecting the recommended decisions and bring it back for a second
  confirmation gate before any tests are written.

## Recommendation summary (for the board)

| Gap | Recommended decision | Why |
|-----|---------------------|-----|
| G1  | **G1-α** — MH-3 tests archive-only behavior; replay stays carry-forward | Honest to actual Phase 1 contract; replay needs its own research |
| G2  | **Correct MH-2** — sequence must include `USER_PROMPT` events to exercise segment_index | Otherwise the test asserts on a non-changing field |
| G3  | **Correct MH-2** — use `SUB_AGENT_START`/`END` events, add stack-mismatch death case | Matches actual API surface, adds high-value death assertion |
| G4  | **G4-β** — print latency histogram to stderr, no on-disk artifact | Simplest; trend analysis is nice-to-have, not load-bearing |
| G5  | **G5-β** — `tests/integration/test_phase1_e2e.py` + lifted fixtures | Consistent with kickoff's implicit naming |

If accepted as-is, the resulting plan ships **5 must-haves** (MH-1
through MH-5) with corrected MH-2/MH-3 scope, no new feature work,
and one new top-level test directory.
