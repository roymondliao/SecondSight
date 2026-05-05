# Kickoff: GUR-99 — Phase 1 End-to-End Integration Test

## Problem Statement

Phase 1 has shipped every component of the observation pipeline as
independent units (hook scripts, API server, raw trace store, events
repository, session tracker, fallback writer, filesystem backfill,
`secondsight` CLI), each with its own test file. **No test exercises
the seams between them as a single composed system.** GUR-99 is the
gate that proves the user-visible install-and-run path actually works:
`secondsight init` → start the server → an agent fires a real hook →
event lands in DB → server crashes → `secondsight sync` recovers.

If GUR-99 is missing or weak, regressions in *any one component* can
silently break the full pipeline while every unit test stays green.
That is the classic "every part passes, the whole is broken" failure
this test exists to catch.

## Evidence

- `tests/scripts/test_hook_fallback.py` already exists with **UT-1, UT-1b,
  UT-2..8, DT-1..9** covering hook script behavior with fake servers,
  one real-server happy path, and one 100-concurrent real-server case.
- No test exercises `secondsight init` → `secondsight serve --daemon` →
  hook fire → `secondsight sync` as a chained sequence. Each piece is
  unit-tested in isolation (`tests/installer/`, `tests/cli/test_serve_daemon.py`,
  `tests/storage/test_filesystem_backfill.py`).
- No test measures **end-to-end hook latency** (bash startup + curl +
  localhost RTT + server work + response). `tests/api/test_latency_contract.py`
  measures only the in-process FastAPI handler timing.
- No test verifies that **`segment_index` and sub-agent nesting** are
  correctly persisted into DB rows when hooks are fired in a realistic
  sequence (PreToolUse → PostToolUse → PreToolUse → ...) for the same
  `session_id` against a live server.
- No test covers the **fallback → server-recovery → backfill** flow as
  one continuous scenario. `tests/storage/test_filesystem_backfill.py`
  tests the backfill module against pre-staged JSONL; the gap is the
  end-to-end "hook wrote fallback because server was down → user starts
  server → user runs `secondsight sync` → DB ends up consistent" path.

## Risk of Inaction

If we ship Phase 1 without GUR-99:

- A future change that subtly breaks the seam (e.g. installer writes
  hooks under a path that `serve --daemon` doesn't bind to, or `sync`
  archives `fallback_events.jsonl` before commit) will pass every unit
  test and fail in production silently — exactly the failure class
  unit tests cannot catch.
- Phase 2 (analysis layer) will be built on a pipeline that has never
  been observed end-to-end. Bugs surfaced during Phase 2 development
  will be ambiguous (analysis vs ingest), expensive to debug.
- The latency claim in SD §3.9.1 (≤7ms hook overhead, <10ms total) is
  unverified. Shipping a number that nobody has measured is theater.

## Scope

### Must-Have (with death conditions)

- **MH-1 — End-to-end hook → server → DB integration test (single event).**
  Drive `scripts/hooks/pre-tool-use.sh` against a real `create_app()`
  server bound to a tmp `SECONDSIGHT_HOME`. Assert: hook exits 0, no
  fallback line, DB has 1 row, raw trace file exists, `segment_index=0`,
  `sub_agent_id` is null/root.
  *Death condition:* if `tests/scripts/test_hook_fallback.py::UT-1`
  is generalized to cover this assertion set, this test becomes
  redundant and should be removed rather than maintained in parallel.

- **MH-2 — End-to-end session tracker integration test (multi-event).**
  Fire a realistic sequence: `session-start` → `pre-tool-use` →
  `post-tool-use` → `pre-tool-use` → `post-tool-use` → `session-end`,
  all sharing one `session_id`. Assert: events table has 6 rows in
  correct `sequence_number` order, `segment_index` increments per
  SD §3.9 rules, sub-agent nesting (when `payload.parent_agent_id`
  is set) yields correct `depth`/`sub_agent_id`.
  *Death condition:* if SessionTracker invariants move into a typed
  state machine with property-based tests in `tests/observation/`,
  the e2e nesting check can drop to a single 2-event smoke.

- **MH-3 — Fallback recovery scenario (server-down → sync).**
  (a) Stop server. (b) Fire 5 hooks → 5 lines in `fallback_events.jsonl`.
  (c) Start server. (d) Run `secondsight sync` (programmatic invocation,
  not subprocess). (e) Assert: DB has 5 rows, fallback file is
  archived to a timestamped `.bak`, re-running `sync` is idempotent
  (no duplicates, no error).
  *Death condition:* obsolete the day fallback is replaced with a local
  durable queue (e.g. SQLite WAL ingest); the recovery semantics
  change shape entirely.

- **MH-4 — Hook latency measurement test (informational, with budget).**
  Run `pre-tool-use.sh` against a live server N=50 times sequentially,
  record total wall-clock per invocation. Assert: **p95 ≤ 50ms** on
  the test machine. Latency is *recorded* (not just asserted) into
  a JSON artifact under `tests/_artifacts/` so CI history shows trend.
  *Death condition:* drop when we have a real production-side latency
  metric (e.g. server-emitted Prometheus histogram); this proxy
  exists only because that metric does not exist yet.

- **MH-5 — Install-and-run lifecycle smoke (CLI composition).**
  In a tmp HOME: run `secondsight init` (programmatic, dry-run first
  then real) → assert hook scripts present and `settings.json` patched
  → run `secondsight serve --daemon` → assert PID file written and
  port listening → fire one hook via `subprocess` → assert DB row →
  run `secondsight serve --stop` → assert process gone, PID file
  removed → run `secondsight status --format json` → assert
  not-running state with correct event counts.
  *Death condition:* this test is the most fragile (subprocess + ports
  + filesystem). If macOS CI flake rate exceeds 2%, downgrade to a
  Linux-only nightly job rather than a per-PR gate.

### Nice-to-Have

- **NH-1** — Replay an existing fixture session under `tests/fixtures/claude_code/`
  through hooks instead of synthetic payloads, to detect adapter
  drift against real Claude Code emissions.
- **NH-2** — Concurrent CLI lifecycle test (`init` running while `serve`
  is being called) — defensive against operator footguns. Defer to
  Phase 2 unless an early bug surfaces.

### Explicitly Out of Scope

- **OoS-1** — Cross-platform CI matrix (Linux + macOS + Windows). Phase 1
  ships macOS-developer-first per `docs/plan_v2.md`; Linux-only CI in
  `2026-05-04_phase4-1-ci-pipeline` is sufficient.
- **OoS-2** — Performance regression budget on `secondsight sync` for
  large fallback files (>10k lines). Tracked separately under storage
  scaling work.
- **OoS-3** — Analysis layer (Phase 2) integration; this test stops at
  "DB has the right rows."
- **OoS-4** — Re-testing component-level behaviors already covered in
  `tests/installer/`, `tests/cli/test_serve_daemon.py`,
  `tests/observation/test_tracker.py`,
  `tests/storage/test_filesystem_backfill.py`,
  `tests/scripts/test_hook_fallback.py` (DT-1..9, UT-2..8). The
  e2e tests **assume those pass** and only test compositions across
  module boundaries.

## North Star

```yaml
metric:
  name: "Phase 1 pipeline end-to-end correctness"
  definition: >
    Probability that a fresh install (secondsight init → serve --daemon)
    correctly persists every hook event into the per-project intelligence.db,
    and recovers all fallback-written events on the next `secondsight sync`,
    measured by green runs of the GUR-99 e2e suite over 30 consecutive CI runs.
  current: unmeasured
  target: ">= 29/30 green (one flake permitted)"
  invalidation_condition: >
    If GUR-99 e2e tests pass but a real user reports the install path
    silently drops events, the test is wrong: it is asserting on something
    other than the user-observable outcome.
  corruption_signature: >
    "Test suite green, but no test ever ran the bash hook script through
     a live network socket against a real server" — i.e. tests degrade into
     in-process Python emulation of the bash script. Detected by spot-check
     on every PR that touches scripts/hooks/*.sh: confirm at least one e2e
     test invokes the .sh file via subprocess, not via a Python helper.

sub_metrics:
  - name: "p95 hook wall-clock latency (e2e bash → response)"
    current: unmeasured
    target: "<= 50ms on developer macbook (single-thread, idle machine)"
    proxy_confidence: medium
    decoupling_detection: >
      Proxy: developer-machine wall-clock. Main: production hook latency.
      Decoupled when CI machine has noisy neighbors or when SD §3.9.1's
      ~7ms estimate doesn't survive a real bash startup measurement.
      Mitigation: record per-run histogram into tests/_artifacts/, alert if
      p95 doubles between runs.

  - name: "Backfill idempotency"
    current: unmeasured
    target: "Re-running `secondsight sync` after a successful run inserts 0 new rows and emits 0 errors"
    proxy_confidence: high
    decoupling_detection: >
      Idempotency at the test level only proves the happy path. Drift
      surfaces when partial-failure backfill (e.g. SIGKILL mid-replay)
      leaves a half-archived .bak. MH-3 covers the happy path only;
      partial-failure recovery is tracked separately in scar reports.
```

## Stakeholders

- **Decision maker:** yuyu_liao (project owner)
- **Impacted teams:** Phase 2 analysis-layer agent (consumes the DB
  this pipeline produces); Phase 4 CI pipeline agent (must wire this
  test into the gate)
- **Damage recipients:**
  - Backend engineer (me, Tianqi): this test suite must be maintained
    when any seam between hook script / API server / storage / CLI
    changes. Adds ~150–250 lines of test code with subprocess and
    network dependencies — these tend to be flaky.
  - CI cycle time: e2e tests with subprocess-spawned servers add
    seconds-to-minutes per PR. Net cost paid by every contributor.

## Step 0 Commitments

1. **Most-wanted shortcut, rejected**: "just write one test that calls
   `client.post('/hook/pre-tool-use')` against the FastAPI TestClient
   for every scenario." Rejected — that bypasses the bash script,
   curl, and the localhost socket, which are exactly the seams we're
   testing. If we don't fork a real shell process, GUR-99 is theater.
2. **This test should NOT run when**: the test environment lacks
   `bash`, `curl`, or `jq`. The test must explicitly skip with a clear
   message naming the missing prerequisite, never silently green-pass.
3. **Silent failure surface this PR closes**:
   - Hook installed but `settings.json` patch lost during install →
     no event ever fires → MH-5 catches by asserting settings.json
     has the SecondSight entry post-`init`.
   - `serve --daemon` writes PID file but binds to wrong port →
     hook fires but goes to fallback → MH-5 catches by asserting
     0 fallback lines after a hook fires post-`serve`.
   - `sync` archives `fallback_events.jsonl` before all DB INSERTs
     committed → next session loses events → MH-3 catches by
     simulating partial failure and asserting `.bak` is only created
     after success (already enforced by `cli/sync.py`; this is the
     regression guard).
   - Session tracker persists `segment_index=0` for all events
     (silently broken counter) → MH-2 catches by asserting
     monotonically increasing `segment_index` for same-session events.
4. **What lives one year from now?**: MH-1, MH-2, MH-3, MH-5 should
   live as long as the `bash hook → API server → SQLite DB` shape
   remains. MH-4 (latency) is explicitly time-bounded — it's a
   placeholder for a real production metric. Document this in the
   test docstring so the next maintainer knows it's removable.
