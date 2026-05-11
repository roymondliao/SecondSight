# Kickoff: GUR-107 — Phase 3A: Data Retention + Observation API

> Samsara research artifact. STEP 0 questions answered before any
> implementation begins. Three prior heartbeats hit the org monthly
> usage limit before producing a plan; this kickoff captures the
> scope reality so the next heartbeat can proceed cleanly.

## Problem Statement

Phase 1 lands raw events on disk and in `events` (per-project SQLite).
Phase 3 needs two things that today do not exist:

1. **Data lifecycle** — without TTL cleanup the per-project store
   grows unboundedly. SD §3.10 fixes the policy (90d raw, 365d
   analysis, per-project override). P3A-11 wires the trigger;
   P3A-12 exposes the manual subcommand.
2. **Observation API** — the dashboard (GUR-106) cannot render a
   session list, segment list, or event detail without REST endpoints.
   P3A-13 closes that gap. The data already lives in `events`; the
   missing piece is the read surface, not the storage.

## STEP 0 — Pre-implementation Questions

### 1. The most expected implementation — and why we should NOT take it

The default move is to ship all three tasks together: TTL config →
cleanup function → trigger after analysis → CLI subcommand → 4 GET
endpoints. **We should not take that path** because:

- **The "after analysis completes" trigger has no analysis pipeline to
  hook into yet.** Phase 2 (GUR-100) is in research only; the
  `analysis_results` and `behavior_flags` tables do not exist. Wiring
  the cleanup into a non-existent post-analysis hook is dead code —
  and `analysis_ttl_days` cannot be applied to tables that do not
  exist.
- **`secondsight cleanup` without a target object is meaningless.**
  The command must enumerate something concrete (project_id,
  session_id) — but the project layer that owns these is fragmented
  across `_home.py`, `registry.py`, and per-project DB engines. A
  hasty CLI ships an inconsistent enumeration.
- **The observation API is independently valuable and unblocks
  GUR-106.** Bundling it with cleanup risks both being delayed by the
  Phase 2 dependency.

### 2. When should this NOT be implemented?

- If Phase 2 (GUR-100) ships an analysis pipeline within the same
  iteration window, the post-analysis cleanup hook becomes the
  natural integration point and cleanup-from-CLI becomes the second
  consumer. In that case, ordering swaps: P3A-11 should follow
  Phase 2 P2-2's analysis-orchestrator landing.
- If single-project MVP scope (memory: dashboard_api_contracts) is
  re-narrowed to "no retention until alpha exit", P3A-11 + P3A-12
  should be deferred entirely. The risk this carries is unbounded
  per-project DB growth on long-lived dev installs — measurable but
  not fatal at MVP traffic (0.5–2 events/sec, SD §3.2).

### 3. Silent-failure surface

Where can this rot quietly?

- **Per-project config override resolution.** `~/.secondsight/config.toml`
  → `~/.secondsight/projects/{pid}/config.toml`. If the loader
  silently picks global defaults whenever the per-project file is
  unreadable (permission, malformed TOML), users will see "cleanup
  ran with the wrong TTL" only when they look at disk usage weeks
  later. Detection lag = weeks. **Mitigation**: explicit log line
  per project on every cleanup invocation showing the resolved
  effective TTL and its source (global / per-project / default).
- **Cleanup vs. concurrent ingest race.** Cleanup deletes raw trace
  files for sessions older than TTL while a long-lived session might
  still be appending events. If cleanup uses `(now - created_at) >
  TTL` on the session row but a hook arrives for that session
  afterwards, ingest fails with `UnsafePathError` only if the
  parent dir was reaped, otherwise it succeeds and orphans an event
  in a half-deleted session. **Mitigation**: TTL boundary is
  `last_event_at`, not `created_at`; cleanup skips sessions with
  `last_event_at` newer than TTL even if `created_at` is older.
- **Observation API leaking event payloads cross-project.** If
  `GET /api/sessions/{id}` does not gate on a project_id query
  param, a caller can probe any session_id across all projects. SD
  §3.7 asserts per-project DB isolation; the API must preserve it.
  Detection lag = "until someone audits". **Mitigation**: every
  observation endpoint takes `project_id` as a required query
  parameter; no cross-project enumeration is possible.
- **CLI `--dry-run` that lies.** A `--dry-run` that computes the
  candidate set differently from the actual run (e.g. by reading
  the FS instead of the DB) silently misreports. **Mitigation**:
  `--dry-run` calls the same enumeration function as real cleanup;
  only the side-effecting writes (DELETE FROM events; os.unlink)
  are gated.

### 4. Will this still be alive in the future?

- TTL cleanup: yes — every long-running observability tool needs
  retention. Stable surface.
- `secondsight cleanup` CLI: yes — operator escape hatch.
- Observation API: yes — frozen contract under SD §10.4 once the
  dashboard ships. The shape (GET sessions/{id}/segments[/{idx}]) is
  already pinned in the dashboard_api_contracts memory.

The high-risk-of-being-deleted piece is the **post-analysis trigger**
in P3A-11: if Phase 2 ships a different analysis lifecycle (e.g.
event-loop-driven instead of session-end-driven), the trigger plumbing
is rework. We can de-risk this by making the cleanup function
publishable from any caller — the trigger plumbing itself is the
cheap part.

## Evidence

- `src/secondsight/storage/events_table.py` defines the only data
  table in scope: `events`. Indexes on `(session_id, sequence_number)`
  and `(session_id, segment_index)` cover the observation API queries
  natively.
- `src/secondsight/storage/raw_trace_store.py` is the sole writer of
  per-event JSON files under `sessions/{session_id}/events/`. It
  already enforces a regex on `session_id` and refuses to escape the
  project root — the cleanup deleter can lean on the same helper.
- `src/secondsight/cli/app.py` is the Typer entry point; existing
  subcommands (`init`, `serve`, `status`, `sync`) follow a
  one-file-per-command layout. `cleanup.py` slots in cleanly.
- `src/secondsight/api/server.py` already mounts a hooks router and
  threads a typed AppState; an `observation` router follows the
  same pattern. No lifespan changes needed.
- `docs/system_design.md` §3.10 fixes retention policy.
  `docs/system_design.md` §10.4 fixes the observation API path
  schema. The `dashboard_api_contracts` memory pins single-project
  MVP, ETag/cursor polling, and local-only bind.
- `changes/2026-05-05_gur-100_phase2-analysis-core/` contains
  kickoff + autopsy only. No `analysis_results` or `behavior_flags`
  tables exist — verified by `find src -name "analysis*"` returning
  zero matches in `storage/`.

## Proposed Scope Split (for board confirmation)

Given the Phase 2 dependency, propose splitting GUR-107 into two
sub-issues:

| Sub-issue | Scope | Blocked? |
|---|---|---|
| **GUR-107a** | Observation API (P3A-13) + raw_traces TTL function + CLI cleanup subcommand (P3A-12 against raw_traces only) + `--dry-run` | **No** |
| **GUR-107b** | analysis_ttl wiring + post-analysis trigger | **Yes — depends on Phase 2 (GUR-100) shipping `analysis_results` table** |

GUR-107a unblocks GUR-106 (dashboard) and lets operators control
disk usage today. GUR-107b ships once the analysis tables land.

If the board prefers to keep GUR-107 atomic, it must remain
in_progress (blocked) until Phase 2 implementation completes — at
which point all three tasks land together but with a multi-week
dependency chain.

## Open Questions for the Board

1. **Confirm the scope split** above (preferred) or hold GUR-107 as
   one atomic unit waiting on Phase 2.
2. **TTL boundary**: SD §3.10 says "session_end + N days". Confirm
   `last_event_at` is the boundary (not `created_at`) per the
   silent-failure mitigation above.
3. **Effective-TTL log line**: confirm cleanup must log resolved TTL
   per project on every invocation (default proposed: yes).
4. **Cross-project enumeration**: confirm every observation endpoint
   requires `project_id` (default proposed: yes, per SD §3.7
   isolation).

## Next Action

Pending board confirmation on the scope split, the next heartbeat
moves to `samsara:planning` to produce `2-plan.md`,
`acceptance.md`, and `tasks/` for **GUR-107a** only.

If the board declines to split, the next heartbeat instead marks
GUR-107 as `blocked` on GUR-100 and links the unblock owner.
