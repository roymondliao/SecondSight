# Task 4: MH-4 — Hook latency wall-clock measurement + p95 ≤ 50ms gate

## Context

Read: `overview.md` for full architecture and decisions.

This task adds `TestMH4LatencyBudget` to `tests/integration/test_phase1_e2e.py`.

**Critical scope decision (G4-β, board-confirmed)**: latency histogram is printed to stderr. No on-disk artifact written. Trend analysis is a nice-to-have, not load-bearing — if a real production-side latency metric exists later, this entire test should be deleted (death condition documented in test docstring).

**Latency budget**: SD §3.9.1 sets a 7ms theoretical target (5ms bash + 1ms HTTP req + 1ms HTTP resp). **The test gate is p95 ≤ 50ms** — a CI-stable proxy. The 50ms number is documented in the kickoff and pre-thinking artifacts and is intentionally generous to avoid flakes from machine load. Do NOT tighten it without revisiting.

The fixtures used (already exist):
- `real_secondsight_server` — yields `{port, home, project_id, session_id}`
- `hook_script`, `run_hook`, `build_env` from `tests/scripts/conftest.py`

For wall-clock measurement, use `time.perf_counter()` around `run_hook(...)`. Each measurement is one full subprocess invocation (bash startup + curl + localhost RTT + server work + response).

## Files

- Modify: `tests/integration/test_phase1_e2e.py` — add `TestMH4LatencyBudget` class
- Test: same file

## Death Test Requirements

- **DT-4.1** — All 50 hook invocations must complete (no `subprocess.TimeoutExpired`). If any timeout, fail with `"subprocess timeout — measurement compromised"`. Never pass on partial data.
- **DT-4.2** — After 50 measurements, p50/p95/p99 are printed to stderr in a single line histogram. The line must contain literal substrings `"p50="`, `"p95="`, `"p99="` so log-grepping is reliable.
- **DT-4.3 (gate)** — p95 ≤ 50ms. Failure message must include the actual p95 value AND a one-line histogram so the operator can see the distribution.

## Implementation Steps

- [ ] Step 1: Write death tests DT-4.1, DT-4.2, DT-4.3. Run — verify red.
- [ ] Step 2: Implement using `time.perf_counter()` deltas around `run_hook(...)`. Use unique `event_id` per iteration so DB does not silently dedupe. Run.
- [ ] Step 3: First green run: record observed p95 on developer machine. If p95 ≥ 30ms, the budget is appropriate. If p95 ≤ 5ms, the budget is too loose and should be re-discussed with the board (do NOT tighten unilaterally — the loose budget is a deliberate choice, not an oversight).
- [ ] Step 4: Stress: run the latency test 10 consecutive times. Note variance.
- [ ] Step 5: Write scar report. Commit.

## MH-4 specifics

```python
class TestMH4LatencyBudget:
    """MH-4: Hook end-to-end wall-clock latency budget.

    Death condition: remove this entire class when production-side latency
    metric (server-emitted Prometheus histogram) exists. This test is a
    CI-stable proxy for SD §3.9.1's theoretical 7ms target; the p95 ≤ 50ms
    gate is intentionally generous.
    """

    def test_mh4_p95_latency_under_budget(
        self, real_secondsight_server: dict[str, Any], capsys
    ) -> None:
        # 50 sequential hooks. Each iteration:
        #   - unique event_id (uuid4().hex)
        #   - record start = time.perf_counter()
        #   - run_hook(pre-tool-use.sh, payload, env=...)
        #   - record dt = (time.perf_counter() - start) * 1000  (ms)
        # Use timeout=2.0 per invocation; on TimeoutExpired, fail with DT-4.1 message.
        # Use statistics.quantiles or sorted-list percentile for p50/p95/p99.
        # Print histogram to stderr via sys.stderr.write or print(..., file=sys.stderr).
        # Format: "MH-4 latency ms: p50=<x> p95=<y> p99=<z> n=50"
        # Assert p95 <= 50.0; failure message must include the histogram.
        ...
```

## Expected Scar Report Items

- Potential shortcut: only measuring the HTTP RTT inside the server process (skip subprocess) to "stabilize" — REJECTED, the whole point is to measure bash + curl + socket cost. The kickoff Step 0 commitment 1 forbids in-process emulation.
- Potential shortcut: tightening p95 to 20ms because "it usually passes at 20" — REJECTED without board approval. Tight budget = high flake rate.
- Potential shortcut: using the same `event_id` across all 50 iterations (DB dedupe makes test green even if events drop) — REJECTED, must use unique ids and assert all 50 land in DB to prove measurements are real.
- Assumption to verify: `time.perf_counter()` resolution is sufficient for sub-millisecond measurements on macOS — yes, ~ns resolution.
- Assumption to verify: pytest's `capsys` does not interfere with `print(..., file=sys.stderr)` — confirm with a small probe test if uncertain. Alternative: use `sys.__stderr__` directly to bypass capture.

## Acceptance Criteria

- Covers: "Unknown outcome - hook latency p95 must remain bounded under measurement"
- Documented as time-bounded in test docstring; remove when production latency metric exists
