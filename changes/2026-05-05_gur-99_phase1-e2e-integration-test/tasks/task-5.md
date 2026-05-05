# Task 5: MH-5 — CLI lifecycle composition (init → serve --daemon → hook → stop → status)

## Context

Read: `overview.md` for full architecture and decisions.

This task adds `TestMH5CliLifecycle` to `tests/integration/test_phase1_e2e.py`.

This is the most fragile of the five must-haves: it spans subprocess fork (`serve --daemon` daemonizes), filesystem state (`~/.claude/hooks/`, `~/.claude/settings.json`, PID file), and a real network bind. Failure modes typically clustered:
- Daemon doesn't start within the timeout (machine load)
- Stale PID file from a previous failed test run
- Port conflict (mitigated by using kernel-assigned port via env)

CLI surface (already exists):
- `secondsight init [--dry-run]` — installs hook scripts to `~/.claude/hooks/`, patches `~/.claude/settings.json` (see `tests/installer/test_hook_install.py` for already-tested behavior)
- `secondsight serve --daemon` — forks a uvicorn process, writes PID file
- `secondsight serve --stop` — reads PID file, signals process, removes PID file
- `secondsight status [--format json]` — reports running/not-running + per-project event counts (see `tests/cli/test_serve_daemon.py` for already-tested behavior)

For init/sync/status, use `typer.testing.CliRunner` (clean, no fork).
For `serve --daemon`, you MUST use `subprocess.Popen(['secondsight', 'serve', '--daemon', ...])` because daemonization forks. Verify the binary is on PATH or invoke as `python -m secondsight serve --daemon ...`.

To avoid `~/.claude/` pollution, redirect HOME (or whatever env var the installer respects) to a tmp directory. Read `src/secondsight/cli/init.py` to confirm which env var or argument controls this — likely `--claude-home` or similar; otherwise `HOME` env override.

The PID file location: confirm by reading `src/secondsight/daemon.py` (already partially read in research; ~15K file).

## Files

- Modify: `tests/integration/test_phase1_e2e.py` — add `TestMH5CliLifecycle` class
- Test: same file

## Death Test Requirements

- **DT-5.1** — `secondsight init --dry-run` does NOT modify `~/.claude/hooks/` OR `~/.claude/settings.json`. After dry-run, real `init` must produce the expected files.
- **DT-5.2** — After `serve --daemon` returns, the port is bound within 5s AND PID file exists with a valid pid number AND `kill -0 <pid>` succeeds. If the PID file exists but port is not bound, fail with "daemon PID file present but port not bound — silent partial start".
- **DT-5.3** — After `serve --stop`, PID file is gone AND port is no longer bound AND `kill -0 <pid>` fails (or returns ESRCH). If PID file lingers, fail with "stop did not clean up PID file".
- **DT-5.4** — `status --format json` after stop must report `running: false` (or platform equivalent), NEVER `running: true`. This is the silent-failure assertion: status must not lie about service availability.

## Implementation Steps

- [ ] Step 1: Read `src/secondsight/cli/init.py` to confirm how to redirect Claude HOME for the test (env var, CLI flag, or both). Record finding in test docstring.
- [ ] Step 2: Read `src/secondsight/daemon.py` to confirm PID file path and start/stop semantics.
- [ ] Step 3: Read `tests/cli/test_serve_daemon.py` to crib subprocess pattern (already-established).
- [ ] Step 4: Write death tests DT-5.1, DT-5.2, DT-5.3, DT-5.4. Run — verify red.
- [ ] Step 5: Write happy-path lifecycle test (one composed flow). Use a `try/finally` finalizer that always attempts `serve --stop` to avoid stale daemons leaking between test runs.
- [ ] Step 6: Run all tests. If flaky, add poll loops with timeouts (never blind sleeps); document any sleep > 0.5s.
- [ ] Step 7: Stress: 10× loop with explicit teardown verification between iterations.
- [ ] Step 8: Write scar report. Commit.

## MH-5 specifics

```python
class TestMH5CliLifecycle:
    """MH-5: Full install-and-run lifecycle composes correctly."""

    def test_mh5_lifecycle_composes_end_to_end(
        self, tmp_path: Path
    ) -> None:
        # Use tmp_path as both SECONDSIGHT_HOME and a fake CLAUDE_HOME.
        # Set environment vars BEFORE invoking CliRunner / subprocess.
        # Use a finalizer (try/finally) to always attempt serve --stop on teardown.
        #
        # Sequence (each step asserts before proceeding):
        #   1. CliRunner: secondsight init --dry-run --claude-home=<fake>
        #      Assert exit 0, no files in <fake>/hooks/, no settings.json.
        #   2. CliRunner: secondsight init --claude-home=<fake>
        #      Assert <fake>/hooks/pre-tool-use.sh exists; <fake>/settings.json
        #      contains the SecondSight hook entry.
        #   3. subprocess.Popen: secondsight serve --daemon --home=<sshome> --port=<assigned>
        #      Poll for PID file (timeout 5s). Poll for port bound (timeout 5s).
        #      If PID file but no port: DT-5.2 failure message.
        #   4. run_hook(pre-tool-use.sh, payload, env=...) → assert exit 0,
        #      sleep 0.3, assert 1 DB row.
        #   5. CliRunner: secondsight serve --stop --home=<sshome>
        #      Assert PID file gone, port no longer bound, kill -0 fails.
        #   6. CliRunner: secondsight status --format json --home=<sshome>
        #      Parse JSON, assert running == False, event_count == 1.
        ...
```

## Expected Scar Report Items

- Potential shortcut: skipping `--dry-run` step to halve runtime — REJECTED; dry-run is part of the documented surface (see GUR-98 exit_criteria) and must be exercised.
- Potential shortcut: using `time.sleep(2.0)` for daemon startup instead of polling — REJECTED; replace with `until <port-bound>: time.sleep(0.05)` capped at 5s.
- Potential shortcut: skipping the `kill -0` death check after stop because "the port being unbound is enough proof" — REJECTED; on macOS the port can briefly remain TIME_WAIT after process exit. The PID-level check is the ground truth.
- Assumption to verify: `secondsight init` accepts a `--claude-home` flag or respects `HOME` env. Read `cli/init.py` to confirm.
- Assumption to verify: `serve --stop` is exit 0 even if PID file is missing (idempotent stop). If not, the test must handle that case.
- Assumption to verify: `status --format json` shape — confirm field names by running the command once during Step 1.

## Acceptance Criteria

- Covers: "Success - CLI lifecycle composes from install to teardown" (happy path)
- Covers: "Silent failure - serve --daemon PID file present but port not bound" (DT-5.2 / DT-5.4)
- Covers: kickoff Step 0 commitment 3 (silent failure path: PID lies about port-bound state)
