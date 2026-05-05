# Problem Autopsy: GUR-99 — Phase 1 End-to-End Integration Test

## original_statement

> Validate the complete observation pipeline works end-to-end.
>
> **Task (P1-14):**
> - P1-14: Simulate Claude Code hook trigger → server receives → filesystem + DB write successful
>
> **Exit criteria (Phase 1 complete gate):**
> - Hook → API server → raw trace written + DB INSERT: full pipeline works
> - Hook latency < 10ms (bash+curl → server → response)
> - Server down → fallback works
> - Backfill mechanism restores DB from fallback
> - Session tracker correctly maintains `segment_index` and sub-agent nesting
> - `secondsight init` installs hook scripts
> - `secondsight serve --daemon` / `--stop` / `status` works
>
> **Ref:** SD 3.9 full pipeline

## reframed_statement

Phase 1 has shipped 8+ independently tested components. GUR-99 is the
**seam-level** test gate: it must prove that the user-visible
install-and-run lifecycle (`secondsight init` → `serve --daemon` →
agent fires hooks → `sync` recovers any fallback events) actually
produces a consistent DB. The exit criteria as written conflate
existing component-level coverage (already green) with the genuinely
missing seam tests. The job is to write only the missing tests, not
to re-test what `tests/scripts/test_hook_fallback.py`,
`tests/installer/`, `tests/cli/test_serve_daemon.py`,
`tests/observation/test_tracker.py`, and
`tests/storage/test_filesystem_backfill.py` already cover.

## translation_delta

```yaml
translation_delta:
  - original: "Simulate Claude Code hook trigger → server receives → filesystem + DB write successful"
    reframed: "Drive the real bash hook script via subprocess against a live FastAPI server bound to a tmp HOME, assert DB row + raw trace file."
    delta: >
      "Simulate" is dangerously vague. A Python TestClient.post call to
      /hook/pre-tool-use also "simulates" the trigger but skips the bash,
      curl, and localhost socket — the seams under test. The reframed
      version forbids in-process emulation. tests/scripts/test_hook_fallback.py::UT-1
      already does this for one happy-path scenario; GUR-99 must not
      duplicate it but extend the assertion surface to segment_index,
      multi-event sequences, and sub-agent nesting.

  - original: "Hook latency < 10ms (bash+curl → server → response)"
    reframed: "Hook end-to-end wall-clock p95 ≤ 50ms on developer macbook, recorded as artifact for trend analysis."
    delta: >
      The original number (<10ms) is from SD §3.9.1's *theoretical*
      breakdown: 5ms bash + 1ms HTTP req + 1ms HTTP resp = 7ms. That
      excludes process fork, Python interpreter spin-up on the server
      side for the first call, and any cold-start cost. A 10ms hard
      threshold will go red on machine load alone, producing flake
      noise. 50ms p95 is a defensible budget that still catches
      regressions of the right magnitude. The "10ms" number stays in
      SD as the design target; the test enforces a looser CI-stable
      proxy.

  - original: "Server down → fallback works"
    reframed: "Hook fires while server is down → JSONL line appears; then server starts, `secondsight sync` runs, DB has the events, fallback file archived, re-run is idempotent."
    delta: >
      "Fallback works" is half a test. The other half — "and the
      fallback events actually end up in the DB after recovery" — is
      where silent loss can hide (sync archiving the file before
      INSERT commit, or sync skipping a malformed envelope without
      logging). The reframed version makes the recovery explicit.

  - original: "Session tracker correctly maintains segment_index and sub-agent nesting"
    reframed: "After firing a 6-event hook sequence (start → 2x pre/post → end) for one session_id, DB rows have monotonically increasing sequence_number, segment_index incremented per SD §3.9 rules, and sub_agent_id/depth match the payload's parent_agent_id when set."
    delta: >
      Original is a property statement; reframed is the observable
      DB state that proves the property. tests/observation/test_tracker.py
      tests the tracker in-process; the gap is "does this state
      survive the fastapi → pipeline.ingest async path → DB INSERT
      round-trip" with concurrent writes.

  - original: "`secondsight init` installs hook scripts"
    reframed: "Already covered by tests/installer/test_hook_install.py + test_claude_settings.py. GUR-99 only adds the lifecycle composition test (init → serve → fire → stop → status)."
    delta: >
      Original implies GUR-99 owns this; reality is that test_hook_install.py
      already covers it at the right layer. Re-implementing it here
      would create a second source of truth for installer correctness
      — bad. GUR-99 takes it as a precondition.

  - original: "`secondsight serve --daemon` / `--stop` / `status` works"
    reframed: "Same — covered by tests/cli/test_serve_daemon.py. GUR-99 owns only the composed lifecycle scenario, not unit coverage of each subcommand."
    delta: >
      Same observation as above; the daemon lifecycle is unit-tested.
      The new test in GUR-99 is the chain (init → serve → hook → stop
      → status), not each subcommand in isolation.
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "If `tests/scripts/test_hook_fallback.py` is generalized to cover MH-1, MH-2, and MH-3 (all five real-server scenarios) directly."
    rationale: >
      That file already imports the real create_app() server and
      drives the bash hook via subprocess. Adding the additional
      assertions there is structurally cleaner than spinning up a new
      tests/integration/ directory that duplicates the conftest
      fixtures (real_secondsight_server, hook_script, build_env).
      If we choose this path, GUR-99 should be closed as "extended
      existing test file rather than create new layer" with the same
      net coverage.

  - condition: "If Phase 1 is descoped or Phase 2 begins before this test exists, and Phase 2 itself adds end-to-end tests that incidentally cover the same seams."
    rationale: >
      Integration tests are most valuable when they catch seam regressions
      *during* development, not retroactively. A retroactive integration
      test that has never gone red is suspicious — it may be testing the
      shape of the implementation, not its behavior. If Phase 2 work
      lands first and exercises these paths, downgrade GUR-99 to a
      narrow latency-only test.

  - condition: "If the bash hook scripts are replaced with a Python entry point (e.g. `secondsight hook pre-tool-use < payload`)."
    rationale: >
      The seams change shape: no more curl, no more bash atomicity
      concerns. Most of the assertions in MH-1/MH-2/MH-3 become
      stale. The test layer should be re-derived rather than
      mechanically ported.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Backend engineer maintaining the test suite"
    cost: >
      ~200 lines of new test code involving subprocess, sockets, and
      filesystem layout. Subprocess+socket tests are the most flaky
      class of test. Will need triage attention when CI flakes,
      especially on first port-already-in-use collisions.

  - who: "CI cycle time / contributor PR latency"
    cost: >
      Each MH adds ~3–10s of wall clock (subprocess spawn + uvicorn
      startup + hook fire). 5 must-haves => +30s on every PR. On
      a tight loop this matters; current full test run is ~minutes,
      so this is a ~10–20% increase.

  - who: "Phase 4 CI pipeline owner"
    cost: >
      Must wire shellcheck, jq, curl as test prerequisites in CI
      base image (or skip the tests there with a loud warning).

  - who: "Future maintainer who reads MH-4 and assumes the latency budget is a contract"
    cost: >
      MH-4's 50ms p95 is a CI-stability proxy, not a product
      contract. Mis-reading it as a hard SLO will block PRs that are
      otherwise correct. Mitigation: docstring on MH-4 must say
      "proxy for SD §3.9.1; remove when production metric exists."
```

## observable_done_state

A reviewer can run `pytest tests/integration/test_phase1_e2e.py -v`
on a clean checkout and see five named test cases (MH-1..MH-5) all
green, plus a generated `tests/_artifacts/gur99_latency.json` with
per-run latency histograms. After the suite passes, deliberately
breaking any seam — e.g. changing `pre-tool-use.sh` to POST to
`/hook/pre_tool_use` (underscore), or making `cli/sync.py` archive
the fallback file before commit — must turn at least one of MH-1..3
red, with a failure message that names the broken seam, not just an
assertion mismatch deep inside the call stack.
