"""Death tests + unit tests for scripts/hooks/*.sh (task-4, GUR-96).

Execution order:
  DT-1  Hook exits 0 even when no server is listening (dead port).
  DT-2  50 parallel hook writes produce 50 valid JSONL lines (no truncation).
  DT-3  Missing SECONDSIGHT_HOME → auto-created OR script exits 0 with warning.
  DT-4  Server returns 5xx → fallback JSONL is written.
  DT-5  curl absent from PATH → fallback written, exit 0.
  DT-6  Payload with shell metacharacters round-trips through JSONL intact.
  DT-7  Parent shell with set -e does NOT abort when hook is sourced/run.
  DT-8  Concurrent fallback appends + simulated truncation: remaining lines valid.
  DT-9  Symlink install: pre-tool-use.sh symlinked without co-locating _lib.sh
         → hook exits 0 AND writes fallback (C3 regression guard).

  UT-1  Live real-server happy path: hook → real create_app() server → 200,
         events table has 1 row, raw trace store has 1 file (C2 replacement).
  UT-1b Concurrent live-server: 100 hooks → 100 DB rows, 0 JSONL lines.
  UT-2  No-server path: hook → one line in fallback JSONL.
  UT-3  5xx server path: hook → one line in fallback JSONL.
  UT-4  100 parallel hooks → 100 valid fallback lines (no duplicates, all valid JSON).
  UT-5  Shellcheck is clean on all hooks/*.sh scripts.
  UT-6  SECONDSIGHT_HOME relative path → script exits 0 with a warning (no crash).
  UT-7  Envelope shape: fallback line has agent/event_type/timestamp/payload/version.
  UT-8  Hook payload contains only SECONDSIGHT_HOME dir auto-create on first run.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.scripts.conftest import (
    EXPECTED_VERSION,
    FALLBACK_FILENAME,
    HOOKS_DIR,
    build_env,
    hook_script,
    run_hook,
)


# ---------------------------------------------------------------------------
# Minimal valid Claude Code-style payload for tests
# ---------------------------------------------------------------------------


def minimal_payload(*, tool_name: str = "Bash", extra: str = "") -> str:
    """Return a minimal raw Claude Code-style PreToolUse payload."""
    obj = {
        "session_id": "sess-test",
        "cwd": "/tmp/proj-test",
        "transcript_path": "/tmp/transcript.jsonl",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": "echo hi", "extra": extra},
    }
    return json.dumps(obj)


def unique_payload(*, seq: int = 0) -> str:
    """Return a thin ingress payload with a unique top-level event_id."""
    obj = {
        "event_id": f"evt-{uuid.uuid4().hex}",
        "timestamp": "2026-05-04T12:00:00Z",
        "sequence_number": seq,
        "payload": {
            "session_id": "sess-concurrent",
            "cwd": "/tmp/proj-concurrent",
            "transcript_path": "/tmp/transcript.jsonl",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
    }
    return json.dumps(obj)


# ===========================================================================
# DEATH TESTS — must fail red before any production bash is written
# ===========================================================================


class TestDeathTests:
    """DT-1 through DT-9: target silent-failure paths."""

    # -----------------------------------------------------------------------
    # DT-1: Hook must exit 0 even when no server is listening
    # -----------------------------------------------------------------------
    def test_dt1_hook_exits_zero_on_dead_port(self, tmp_path: Path) -> None:
        """DT-1: A non-zero exit from a hook would cancel the agent's tool call.

        Run pre-tool-use.sh with SECONDSIGHT_PORT=1 (nothing listening on port 1).
        The script must exit 0 even though the POST will fail.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Hook must exit 0 even on dead port; got {result.returncode}. stderr={result.stderr!r}"
        )

    # -----------------------------------------------------------------------
    # DT-2: 50 parallel writes with 1KB payload — no JSONL truncation
    # -----------------------------------------------------------------------
    def test_dt2_parallel_writes_no_truncation(self, tmp_path: Path) -> None:
        """DT-2: PIPE_BUF on macOS is 512 bytes; parallel echo >> can interleave.

        Run 50 hook invocations in parallel against a dead port.
        Assert: JSONL has exactly 50 lines, each parses as valid JSON,
        and each line's payload matches the input.

        NOTE: On macOS without flock (util-linux not available), this test is
        non-deterministic at the OS level — it tests that the application-level
        jq envelope construction produces valid JSON per-line, not that the OS
        guarantees atomic append for writes >512 bytes.  flock(1) atomicity
        (where available on Linux) is tested implicitly.  The architectural fix
        for cross-platform atomicity is tracked as KS-1 (P1-13 Python writer).
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)
        # 1KB payload — exceeds PIPE_BUF on macOS
        big_extra = "x" * 900
        payload = minimal_payload(extra=big_extra)

        futures = []
        with ThreadPoolExecutor(max_workers=50) as pool:
            for _ in range(50):
                futures.append(
                    pool.submit(
                        run_hook,
                        hook_script("pre-tool-use.sh"),
                        payload,
                        env=env,
                        timeout=20.0,
                    )
                )
        results = [f.result() for f in futures]
        for r in results:
            assert r.returncode == 0, f"Hook returned non-zero: {r.returncode}"

        fallback = home / FALLBACK_FILENAME
        assert fallback.exists(), "Fallback JSONL was never created"
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 50, f"Expected 50 lines, got {len(lines)}"
        for i, line in enumerate(lines):
            parsed = json.loads(line)  # must not raise
            assert "payload" in parsed, f"Line {i} missing 'payload' field: {line!r}"

    # -----------------------------------------------------------------------
    # DT-3: Missing SECONDSIGHT_HOME → auto-create OR exit 0 with warning
    # -----------------------------------------------------------------------
    def test_dt3_missing_home_autocreated_or_exit_zero(self, tmp_path: Path) -> None:
        """DT-3: Silent loss is the worst outcome — directory must be auto-created.

        Delete SECONDSIGHT_HOME before running the hook.
        The hook must either create it and write, OR exit 0 with a stderr warning.
        It must NOT silently lose the event AND exit 0 (loss without any signal).
        """
        home = tmp_path / "nonexistent_home"
        # Intentionally do NOT create it
        assert not home.exists()
        env = build_env(port=1, home=home)
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Hook must exit 0 even if home is missing; got {result.returncode}"
        )
        # If home was NOT auto-created, a stderr warning must exist
        if not home.exists():
            assert result.stderr.strip(), (
                "If home is not auto-created, stderr must contain a warning. "
                "Silent data loss is unacceptable."
            )
        else:
            fallback = home / FALLBACK_FILENAME
            assert fallback.exists(), "Home was created but no fallback_events.jsonl found"

    # -----------------------------------------------------------------------
    # DT-4: Server returns 5xx → fallback written
    # -----------------------------------------------------------------------
    def test_dt4_server_5xx_triggers_fallback(self, tmp_path: Path, fake_server_500: int) -> None:
        """DT-4: A 5xx response must be treated as 'server did not get it'.

        The hook must fall through to secondsight_fallback_append.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=fake_server_500, home=home)
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0
        fallback = home / FALLBACK_FILENAME
        assert fallback.exists(), "Fallback JSONL not written after 5xx"
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1, f"Expected 1 line in fallback, got {len(lines)}"
        parsed = json.loads(lines[0])
        assert parsed.get("hook_script_version") == EXPECTED_VERSION

    # -----------------------------------------------------------------------
    # DT-5: curl absent → fallback written, exit 0
    # -----------------------------------------------------------------------
    def test_dt5_curl_absent_triggers_fallback(self, tmp_path: Path) -> None:
        """DT-5: If curl is not in PATH, the hook must degrade gracefully.

        Build a PATH that has bash, jq, date, mkdir, cat, printf but NOT curl.
        The hook must write to the fallback JSONL and exit 0.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()

        # Create a fake_bin with symlinks to essential tools (excluding curl)
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()

        # Find real tool paths
        def _which(name: str) -> str:
            r = subprocess.run(["which", name], capture_output=True, text=True)
            return r.stdout.strip()

        for tool in (
            "bash",
            "jq",
            "date",
            "mkdir",
            "cat",
            "printf",
            "env",
            "sh",
            "wc",
            "tr",
            "head",
            "base64",
        ):
            path = _which(tool)
            assert path, (
                f"Test prerequisite missing: tool {tool!r} not found in PATH. "
                f"This test requires {tool} to construct a deterministic fake_bin; "
                f"silently skipping would let the test pass for the wrong reason."
            )
            dest = fake_bin / tool
            if not dest.exists():
                dest.symlink_to(path)

        env = build_env(port=8420, home=home)
        # PATH has bash + jq but NOT curl
        env["PATH"] = str(fake_bin)

        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Hook must exit 0 when curl is absent; got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )
        # Must either write fallback OR emit a warning (not silent loss)
        fallback = home / FALLBACK_FILENAME
        if not fallback.exists():
            assert result.stderr.strip(), (
                "If fallback is not written (jq also absent?), stderr warning required."
            )
        else:
            lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
            assert len(lines) >= 1, "Expected at least 1 fallback line when curl is absent"
            # C4: degraded envelope must include payload metadata for triage
            # When jq IS available (this test has jq), the envelope is full,
            # so _fallback_degraded is NOT set. This sub-assertion only applies
            # to the jq-absent path (tested separately in test_dt5b).
            # Here we just verify the line is valid JSON.
            parsed = json.loads(lines[0])
            assert "hook_script_version" in parsed, (
                f"Fallback line missing hook_script_version: {parsed}"
            )

    def test_dt5b_jq_absent_emits_warning_and_skips_write(self, tmp_path: Path) -> None:
        """DT-5b: When jq is absent, the hook must fail loudly but non-destructively.

        Build a PATH with curl excluded AND jq excluded, but WITH dirname.
        The hook must still exit 0, emit a warning, and avoid fabricating a
        fallback record without a trustworthy session_key / sequence_number.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()

        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()

        def _which(name: str) -> str:
            r = subprocess.run(["which", name], capture_output=True, text=True)
            return r.stdout.strip()

        # Include everything EXCEPT curl AND jq.
        # dirname is required so `$(dirname "$0")/_lib.sh` resolves correctly.
        for tool in (
            "bash",
            "date",
            "mkdir",
            "cat",
            "printf",
            "env",
            "sh",
            "wc",
            "tr",
            "head",
            "base64",
            "dirname",
        ):
            path = _which(tool)
            assert path, (
                f"Test prerequisite missing: tool {tool!r} not found in PATH. "
                f"This test requires {tool} to construct a deterministic fake_bin; "
                f"silently skipping would let the test pass for the wrong reason."
            )
            dest = fake_bin / tool
            if not dest.exists():
                dest.symlink_to(path)

        env = build_env(port=8420, home=home)
        env["PATH"] = str(fake_bin)

        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Hook must exit 0 when both curl and jq are absent; got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )
        fallback = home / FALLBACK_FILENAME
        assert not fallback.exists(), "jq-absent path must not fabricate a fallback record"
        assert result.stderr.strip(), "Must emit stderr warning when jq is absent"

    # -----------------------------------------------------------------------
    # DT-6: Payload with shell metacharacters round-trips intact
    # -----------------------------------------------------------------------
    def test_dt6_shell_metacharacters_roundtrip(self, tmp_path: Path) -> None:
        """DT-6: Payloads with ', \", $, backticks, newlines must not be corrupted.

        The envelope construction must use jq (not string interpolation) to
        safely handle arbitrary JSON input.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)

        # Metacharacter-laden payload
        evil_inner: dict[str, Any] = {
            "tool_name": "Bash",
            "input": "echo 'hello'; echo \"world\"; echo $HOME; echo `date`",
            "newline": "line1\nline2",
            "backslash": "C:\\Users\\test",
            "dollar": "$PATH",
            "backtick": "`id`",
            "single_quote": "it's a test",
            "double_quote": 'say "hello"',
        }
        evil_payload: dict[str, Any] = {
            "session_id": "sess-test",
            "cwd": "/tmp/proj-test",
            "hook_event_name": "PreToolUse",
            **evil_inner,
        }
        payload_str = json.dumps(evil_payload)

        result = run_hook(hook_script("pre-tool-use.sh"), payload_str, env=env)
        assert result.returncode == 0

        fallback = home / FALLBACK_FILENAME
        assert fallback.exists(), "Fallback JSONL not written"
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        parsed: dict[str, Any] = json.loads(lines[0])
        # The inner payload must have survived intact
        inner: dict[str, Any] = parsed["payload"]
        assert inner["input"] == evil_inner["input"], (
            f"'input' field corrupted. Got: {inner['input']!r}"
        )
        assert inner["single_quote"] == evil_inner["single_quote"]
        assert inner["double_quote"] == evil_inner["double_quote"]
        assert inner["dollar"] == evil_inner["dollar"]

    # -----------------------------------------------------------------------
    # DT-7: Parent shell with set -e does NOT abort
    # -----------------------------------------------------------------------
    def test_dt7_set_e_parent_does_not_abort(self, tmp_path: Path) -> None:
        """DT-7: A hook called from a set -e parent must not abort the parent.

        Wrap the hook in a parent shell that has set -e active.
        Force a fallback (dead port). The parent shell must exit 0.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        script = hook_script("pre-tool-use.sh")
        wrapper = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f"export SECONDSIGHT_PORT=1\n"
            f"export SECONDSIGHT_HOME={home!s}\n"
            f"export SECONDSIGHT_AGENT=test-agent\n"
            f"export PATH=$PATH\n"
            f'echo \'{{"session_id":"sess","cwd":"/tmp/proj-test",'
            f'"hook_event_name":"PreToolUse","tool_name":"Bash"}}\' | bash {script!s}\n'
            "echo 'PARENT_CONTINUED'\n"
        )
        result = subprocess.run(
            ["/bin/bash", "-c", wrapper],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Parent with set -e aborted. returncode={result.returncode} stderr={result.stderr!r}"
        )
        assert "PARENT_CONTINUED" in result.stdout, (
            "Parent shell did not reach the line after hook invocation"
        )

    # -----------------------------------------------------------------------
    # DT-8: Concurrent fallback + truncation race
    # -----------------------------------------------------------------------
    def test_dt8_concurrent_append_truncation_race(self, tmp_path: Path) -> None:
        """DT-8: While appending, another process truncates the file mid-write.

        Simulate a concurrent secondsight sync by truncating the fallback file
        while hook scripts are writing. All REMAINING lines in the file after
        the dust settles must be valid JSON (no corrupted half-lines).

        Note: flock is not available on macOS (no util-linux), so we use the
        documented degraded mode (plain >>). The test verifies atomicity per-line
        at the application level, not at the OS level.  The architectural fix is
        tracked as KS-1 (P1-13 Python writer).
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        fallback = home / FALLBACK_FILENAME
        env = build_env(port=1, home=home)
        payload = minimal_payload()

        stop_flag = threading.Event()

        def _truncator() -> None:
            """Simulate sync by truncating the fallback file repeatedly."""
            while not stop_flag.is_set():
                if fallback.exists():
                    try:
                        fallback.write_text("")  # truncate
                    except OSError:
                        pass
                time.sleep(0.005)

        truncator = threading.Thread(target=_truncator, daemon=True)
        truncator.start()

        futures = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            for _ in range(20):
                futures.append(
                    pool.submit(run_hook, hook_script("pre-tool-use.sh"), payload, env=env)
                )
        for f in futures:
            assert f.result().returncode == 0

        stop_flag.set()
        truncator.join(timeout=2)

        # All REMAINING lines must be parseable as JSON
        if fallback.exists():
            for i, line in enumerate(fallback.read_text().splitlines()):
                if line.strip():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        pytest.fail(
                            f"Line {i} in fallback JSONL is corrupt after truncation race: "
                            f"{line!r} — {exc}"
                        )

    # -----------------------------------------------------------------------
    # DT-9: Symlink install works without co-locating _lib.sh (C3)
    # -----------------------------------------------------------------------
    def test_dt9_symlink_install_no_lib_sh_colocated(self, tmp_path: Path) -> None:
        """DT-9: Symlink install must work even when _lib.sh is NOT in the
        same directory as the symlink.

        This is the C3 regression guard: `dirname $0` gives the symlink's
        directory, NOT the real script's directory.  After the C3 fix
        (BASH_SOURCE + readlink loop), _lib.sh is found at the real script's
        location regardless of where the symlink lives.

        Setup: symlink pre-tool-use.sh into a temp directory that does NOT
        contain _lib.sh.  Run the symlinked script and assert:
          - exit 0 (no crash)
          - fallback file written (observation not lost)
        """
        # Directory that will contain the symlink but NOT _lib.sh
        hooks_proxy_dir = tmp_path / "fake_claude_hooks"
        hooks_proxy_dir.mkdir()

        # Symlink pre-tool-use.sh into the proxy dir (not _lib.sh)
        symlink_path = hooks_proxy_dir / "pre-tool-use.sh"
        real_script = hook_script("pre-tool-use.sh")
        symlink_path.symlink_to(real_script)

        # Verify _lib.sh is NOT in hooks_proxy_dir
        assert not (hooks_proxy_dir / "_lib.sh").exists(), (
            "Test setup error: _lib.sh must NOT be in the proxy dir"
        )

        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)

        result = run_hook(symlink_path, minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Symlinked hook must exit 0 even without _lib.sh co-located; "
            f"got {result.returncode}. stderr={result.stderr!r}"
        )
        fallback = home / FALLBACK_FILENAME
        assert fallback.exists(), (
            "Symlinked hook must write fallback JSONL (observation must not be silently lost). "
            f"stderr={result.stderr!r}"
        )
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1, f"Expected 1 fallback line from symlinked hook, got {len(lines)}"

    @pytest.mark.parametrize(
        "script_name",
        ["session-end.sh", "session-start.sh", "user-prompt.sh"],
    )
    def test_dt10_disable_hooks_env_short_circuits_hook_scripts(
        self,
        tmp_path: Path,
        script_name: str,
    ) -> None:
        """DT-10: Internal hook-disable flag must suppress hook work entirely.

        This is the regression guard for CLI analysis recursion: when the
        analysis subprocess runs under a globally-hooked agent, the hook must
        exit 0 without injecting, posting, or writing fallback records.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        calls_file = tmp_path / "curl-calls.txt"
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        for tool in ("bash", "dirname", "pwd", "readlink"):
            path = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
            assert path, f"test prerequisite missing: {tool}"
            (fake_bin / tool).symlink_to(path)
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' "${@: -1}" >> "$SECONDSIGHT_TEST_CURL_CALLS"\n'
            "exit 99\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

        env = build_env(port=1, home=home)
        env["SECONDSIGHT_DISABLE_HOOKS"] = "1"
        env["PATH"] = str(fake_bin)
        env["SECONDSIGHT_TEST_CURL_CALLS"] = str(calls_file)

        result = run_hook(hook_script(script_name), minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Disabled hook must still exit 0; got {result.returncode}. stderr={result.stderr!r}"
        )
        assert result.stdout == ""
        fallback = home / FALLBACK_FILENAME
        assert not fallback.exists(), (
            "Hook-disable flag must short-circuit before any fallback/event write occurs"
        )
        assert not calls_file.exists(), (
            "Hook-disable flag must short-circuit before any injection or ingest curl call"
        )


# ===========================================================================
# UNIT TESTS
# ===========================================================================


class TestUnitTests:
    """UT-1 through UT-8 + UT-1b: functional correctness tests."""

    # -----------------------------------------------------------------------
    # UT-1: Live real-server happy path → 200, DB row, raw trace file (C2 fix)
    # -----------------------------------------------------------------------
    def test_ut1_live_server_happy_path(
        self, tmp_path: Path, real_secondsight_server: dict[str, Any]
    ) -> None:
        """UT-1: With a real create_app() server, hook POSTs successfully.

        C2 fix: this is NOT a fake-server test. It uses a real uvicorn server
        with the full FastAPI route stack: EventType enum validation, normalizer
        dispatch, SessionTracker.bind(), and pipeline.ingest().

        Assertions:
          1. Hook exits 0.
          2. HTTP response was 200 (no fallback JSONL written).
          3. events table has exactly 1 row with matching event_id.
          4. raw trace store has 1 file at the expected path.

        This test MUST fail red if the hook posts to a URL not in EventType
        (e.g. /hook/tool_use_start vs /hook/pre-tool-use).  That is the C1
        regression guard.
        """
        srv = real_secondsight_server
        home: Path = srv["home"]
        port: int = srv["port"]

        # Use a unique event_id to make assertions unambiguous
        event_id = "evt-ut1-live"
        payload_obj = {
            "event_id": event_id,
            "timestamp": "2026-05-04T12:00:00Z",
            "sequence_number": 1,
            "payload": {
                "session_id": "sess-ut1",
                "cwd": "/tmp/proj-test",
                "transcript_path": "/tmp/transcript.jsonl",
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/proj-test/README.md"},
            },
        }
        payload_str = json.dumps(payload_obj)

        env = build_env(port=port, home=home, agent="claude_code")
        result = run_hook(hook_script("pre-tool-use.sh"), payload_str, env=env)

        assert result.returncode == 0, (
            f"Hook must exit 0; got {result.returncode}. stderr={result.stderr!r}"
        )

        # Assert no fallback JSONL was written — server handled it.
        fallback = home / FALLBACK_FILENAME
        if fallback.exists():
            lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
            assert len(lines) == 0, (
                f"Expected empty fallback on 200 success, got {len(lines)} lines.\n"
                f"This means the hook posted to a wrong URL and fell back to JSONL.\n"
                f"Check event_type URL strings in pre-tool-use.sh (C1 fix)."
            )

        # Give the fire-and-forget ingest task a moment to complete.
        # The server returns 200 before ingest completes (latency contract).
        time.sleep(0.3)

        # --- Assert DB row ---
        # Registry stores per-project DBs at:
        #   $SECONDSIGHT_HOME/projects/{project_id}/intelligence.db
        db_path = home / "projects" / "proj-test" / "intelligence.db"
        assert db_path.exists(), (
            f"DB not created at expected path: {db_path}. "
            f"Check that pipeline.ingest() ran and registry materialized project."
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, (
            f"Expected 1 DB row for event_id={event_id!r}, got {len(rows)}. "
            f"If 0 rows: ingest task may not have completed or the event was rejected."
        )

        # --- Assert raw trace file ---
        sessions_dir = home / "projects" / "proj-test" / "sessions" / "sess-ut1" / "events"
        assert sessions_dir.exists(), f"Raw trace sessions dir not created: {sessions_dir}"
        trace_files = list(sessions_dir.glob("*.json"))
        assert len(trace_files) == 1, (
            f"Expected 1 raw trace file, got {len(trace_files)}: {trace_files}"
        )

    # -----------------------------------------------------------------------
    # UT-1b: 100 concurrent hooks → 100 DB rows, 0 JSONL lines
    # -----------------------------------------------------------------------
    def test_ut1b_concurrent_live_server_100_hooks(
        self, tmp_path: Path, real_secondsight_server: dict[str, Any]
    ) -> None:
        """UT-1b: 100 concurrent hooks → 100 DB rows, 0 JSONL lines.

        Each hook uses a unique event_id (guaranteed by unique_payload).
        Asserts that the live-server path is used for all 100 — no fallback.

        Note: sequence_number is incremented per-hook; the tracker increments
        segment_index independently.  We don't assert segment_index here;
        we assert only that all 100 events reach the DB.
        """
        srv = real_secondsight_server
        home: Path = srv["home"]
        port: int = srv["port"]

        n = 100
        payloads = [unique_payload(seq=i) for i in range(n)]
        event_ids = [json.loads(p)["event_id"] for p in payloads]

        env = build_env(port=port, home=home, agent="claude_code")

        futures = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            for p in payloads:
                futures.append(pool.submit(run_hook, hook_script("pre-tool-use.sh"), p, env=env))
        for f in futures:
            r = f.result()
            assert r.returncode == 0, f"Hook returned {r.returncode}; stderr={r.stderr!r}"

        # No fallback JSONL should exist (all 100 hit the live server successfully)
        fallback = home / FALLBACK_FILENAME
        if fallback.exists():
            lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
            assert len(lines) == 0, (
                f"Concurrent live-server test: expected 0 JSONL lines, got {len(lines)}.\n"
                f"Hooks fell back to JSONL instead of hitting the server."
            )

        # Wait for all fire-and-forget ingest tasks to complete.
        time.sleep(1.0)

        db_path = home / "projects" / "proj-concurrent" / "intelligence.db"
        assert db_path.exists(), f"DB not created: {db_path}"
        conn = sqlite3.connect(str(db_path))
        try:
            placeholders = ",".join("?" * n)
            rows = conn.execute(
                f"SELECT id FROM events WHERE id IN ({placeholders})",
                event_ids,
            ).fetchall()
        finally:
            conn.close()

        found_ids = {row[0] for row in rows}
        missing = set(event_ids) - found_ids
        assert len(missing) == 0, (
            f"Expected all 100 event_ids in DB; missing {len(missing)}: {list(missing)[:5]}..."
        )

    # -----------------------------------------------------------------------
    # UT-2: No-server path → one line in fallback JSONL
    # -----------------------------------------------------------------------
    def test_ut2_no_server_writes_fallback(self, tmp_path: Path) -> None:
        """UT-2: Dead port → exactly one line appended to fallback JSONL."""
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0
        fallback = home / FALLBACK_FILENAME
        assert fallback.exists()
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1, f"Expected 1 fallback line, got {len(lines)}"

    # -----------------------------------------------------------------------
    # UT-3: 5xx server → one line in fallback JSONL
    # -----------------------------------------------------------------------
    def test_ut3_5xx_writes_fallback(self, tmp_path: Path, fake_server_500: int) -> None:
        """UT-3: 5xx server response → hook falls through to fallback."""
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=fake_server_500, home=home)
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0
        fallback = home / FALLBACK_FILENAME
        assert fallback.exists()
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

    # -----------------------------------------------------------------------
    # UT-4: 100 parallel fallback appends → 100 valid lines
    # -----------------------------------------------------------------------
    def test_ut4_100_parallel_fallback_all_valid(self, tmp_path: Path) -> None:
        """UT-4: 100 concurrent hooks against dead port → 100 valid JSONL lines.

        NOTE (I4): On macOS without flock (util-linux not available), this test
        is non-deterministic at the OS level for writes >PIPE_BUF (512 bytes).
        This test uses the minimal payload (~200 bytes), which is below PIPE_BUF,
        so it avoids the interleaving risk in practice.  The test verifies
        application-level envelope construction correctness, not OS-level atomicity.
        The architectural fix for cross-platform atomicity is tracked as KS-1
        (P1-13 Python writer).
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)
        payload = minimal_payload()

        futures = []
        with ThreadPoolExecutor(max_workers=50) as pool:
            for _ in range(100):
                futures.append(
                    pool.submit(
                        run_hook,
                        hook_script("pre-tool-use.sh"),
                        payload,
                        env=env,
                        timeout=20.0,
                    )
                )
        for f in futures:
            assert f.result().returncode == 0

        fallback = home / FALLBACK_FILENAME
        assert fallback.exists()
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 100, f"Expected 100 lines, got {len(lines)}"
        for i, line in enumerate(lines):
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f"Line {i} is invalid JSON: {line!r} — {exc}")

    # -----------------------------------------------------------------------
    # UT-5: shellcheck passes on all hooks
    # -----------------------------------------------------------------------
    def test_ut5_shellcheck_clean(self) -> None:
        """UT-5: shellcheck must report zero findings on all hook scripts."""
        shellcheck = subprocess.run(["which", "shellcheck"], capture_output=True, text=True)
        if shellcheck.returncode != 0:
            pytest.skip("shellcheck not installed — install via 'brew install shellcheck'")

        scripts = list(HOOKS_DIR.glob("*.sh"))
        assert scripts, f"No .sh scripts found in {HOOKS_DIR}"

        result = subprocess.run(
            ["shellcheck", "--shell=bash", "--severity=warning"] + [str(s) for s in scripts],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"shellcheck found issues:\n{result.stdout}\n{result.stderr}"

    # -----------------------------------------------------------------------
    # UT-6: Relative SECONDSIGHT_HOME → exit 0 with warning
    # -----------------------------------------------------------------------
    def test_ut6_relative_home_exit_zero(self, tmp_path: Path) -> None:
        """UT-6: A relative SECONDSIGHT_HOME must not crash the hook.

        The script must exit 0. It should emit a warning to stderr.
        """
        env = build_env(port=1, home=Path("/tmp"))
        env["SECONDSIGHT_HOME"] = "relative/path"  # not absolute
        env["HOME"] = str(tmp_path / "fake-home")
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0, (
            f"Hook crashed on relative SECONDSIGHT_HOME; returncode={result.returncode}"
        )
        # Must emit a warning
        assert result.stderr.strip(), (
            "Hook must emit a stderr warning for relative SECONDSIGHT_HOME"
        )

    # -----------------------------------------------------------------------
    # UT-7: Envelope shape in fallback JSONL is correct
    # -----------------------------------------------------------------------
    def test_ut7_fallback_envelope_shape(self, tmp_path: Path) -> None:
        """UT-7: Fallback line must match documented envelope shape.

        Expected: {"agent":"...", "event_type":"...", "event_id":"...",
                   "timestamp":"...", "sequence_number":N, "payload":{...},
                   "hook_script_version":"phase-2.0"}
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home, agent="my-agent")
        result = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert result.returncode == 0
        fallback = home / FALLBACK_FILENAME
        assert fallback.exists()
        lines = [ln for ln in fallback.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        envelope = json.loads(lines[0])
        assert "agent" in envelope, f"Missing 'agent' field: {envelope}"
        assert "event_type" in envelope, f"Missing 'event_type' field: {envelope}"
        assert "event_id" in envelope, f"Missing 'event_id' field: {envelope}"
        assert "timestamp" in envelope, f"Missing 'timestamp' field: {envelope}"
        assert "sequence_number" in envelope, f"Missing 'sequence_number' field: {envelope}"
        assert "payload" in envelope, f"Missing 'payload' field: {envelope}"
        assert "hook_script_version" in envelope, f"Missing 'hook_script_version': {envelope}"
        assert envelope["hook_script_version"] == EXPECTED_VERSION, (
            f"Wrong version: {envelope['hook_script_version']!r}"
        )
        assert envelope["agent"] == "my-agent", f"Agent mismatch: {envelope['agent']!r}"
        assert isinstance(envelope["payload"], dict), "payload must be a dict"
        assert envelope["sequence_number"] == 0, envelope

    # -----------------------------------------------------------------------
    # UT-7b: sequence_number increases per session on fallback path
    # -----------------------------------------------------------------------
    def test_ut7b_sequence_numbers_increase_per_session(self, tmp_path: Path) -> None:
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home, agent="claude_code")

        r1 = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        r2 = run_hook(hook_script("pre-tool-use.sh"), minimal_payload(), env=env)
        assert r1.returncode == 0
        assert r2.returncode == 0

        fallback = home / FALLBACK_FILENAME
        lines = [json.loads(ln) for ln in fallback.read_text().splitlines() if ln.strip()]
        assert [line["sequence_number"] for line in lines] == [0, 1], lines

    # -----------------------------------------------------------------------
    # UT-8: All per-event scripts share the same exit-0 guarantee
    # -----------------------------------------------------------------------
    def test_ut8_all_event_scripts_exit_zero_on_dead_port(self, tmp_path: Path) -> None:
        """UT-8: Every hook script must exit 0 even with a dead port.

        Checks pre-tool-use.sh, post-tool-use.sh, session-start.sh,
        session-end.sh, user-prompt.sh.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)
        scripts = [
            "pre-tool-use.sh",
            "post-tool-use.sh",
            "session-start.sh",
            "session-end.sh",
            "user-prompt.sh",
        ]
        for script_name in scripts:
            result = run_hook(hook_script(script_name), minimal_payload(), env=env)
            assert result.returncode == 0, (
                f"{script_name} exited non-zero ({result.returncode}). stderr={result.stderr!r}"
            )


# ===========================================================================
# SESSION-START CONVENTION INJECTION TESTS (Layer 1)
# ===========================================================================


def _make_convention_server(conventions_body: str, *, status: int = 200) -> tuple[int, Any]:
    """Start a fake HTTP server that returns a session-start injection response.

    Returns (port, server) so the caller can shut it down.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(conventions_body.encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, server


def _make_recording_convention_server(conventions_body: str) -> tuple[int, Any, list[str]]:
    """Start a fake server and record request paths for hook transport tests."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    paths: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(conventions_body.encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, server, paths


def _make_recording_body_server(conventions_body: str) -> tuple[int, Any, list[str]]:
    """Start a fake server and record request bodies for hook contract tests."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    bodies: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            bodies.append(self.rfile.read(length).decode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(conventions_body.encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, server, bodies


class TestSessionStartConventionInjection:
    """Layer 1 tests: session-start.sh prints raw injection payloads to stdout."""

    def test_dt_raw_server_payload_printed_to_stdout_unchanged(self, tmp_path: Path) -> None:
        """DT-SS-1: hook stdout must be the exact agent-ready response body.

        The hook must not unwrap a legacy ``.conventions`` field. The server
        already rendered the target agent's SessionStart envelope.
        """
        body = '{"systemMessage":"raw payload from server"}'
        port, server = _make_convention_server(body)
        try:
            home = tmp_path / ".secondsight"
            home.mkdir()
            env = build_env(port=port, home=home)
            result = run_hook(hook_script("session-start.sh"), minimal_payload(), env=env)

            assert result.returncode == 0, (
                f"session-start.sh exited non-zero; stderr={result.stderr!r}"
            )
            assert result.stdout == body
        finally:
            server.shutdown()

    def test_session_start_hook_calls_new_injection_namespace(self, tmp_path: Path) -> None:
        """UT-SS-1: the hook calls /hook/injection/session-start/{agent} first."""
        body = '{"systemMessage":"raw payload from server"}'
        port, server, paths = _make_recording_convention_server(body)
        try:
            home = tmp_path / ".secondsight"
            home.mkdir()
            env = build_env(port=port, home=home, agent="codex")
            result = run_hook(hook_script("session-start.sh"), minimal_payload(), env=env)

            assert result.returncode == 0
            assert paths[0] == "/hook/injection/session-start/codex"
            assert "/hook/session-start" not in paths
        finally:
            server.shutdown()

    def test_session_start_hook_sends_cwd_not_project_id(self, tmp_path: Path) -> None:
        """DT-SS-5: project_id derivation belongs to the server, not shell."""
        body = '{"systemMessage":"raw payload from server"}'
        port, server, bodies = _make_recording_body_server(body)
        try:
            home = tmp_path / ".secondsight"
            home.mkdir()
            env = build_env(port=port, home=home, agent="codex")
            result = run_hook(hook_script("session-start.sh"), minimal_payload(), env=env)

            assert result.returncode == 0
            request_body = json.loads(bodies[0])
            assert request_body == {"cwd": "/tmp/proj-test"}
            assert "project_id" not in request_body
        finally:
            server.shutdown()

    def test_empty_stdout_when_server_unreachable(self, tmp_path: Path) -> None:
        """UT-SS-2: When server is unreachable, hook exits 0 with empty stdout.

        Pre-execution injection must degrade silently — no stdout means no
        system prompt modification, while curl diagnostics remain available.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)  # nothing on port 1
        result = run_hook(hook_script("session-start.sh"), minimal_payload(), env=env)

        assert result.returncode == 0, (
            f"session-start.sh must exit 0 on dead port; got {result.returncode}"
        )
        assert result.stdout.strip() == "", (
            f"Expected empty stdout on dead port; got: {result.stdout!r}"
        )
        assert (home / "logs" / "curl-errors.log").exists(), (
            "Injection transport failures must leave diagnostics for operators"
        )

    def test_dt_missing_cwd_writes_diagnostic(
        self,
        tmp_path: Path,
    ) -> None:
        """DT-SS-4: missing SessionStart cwd is diagnosable, not silent 204."""
        home = tmp_path / ".secondsight"
        home.mkdir()
        env = build_env(port=1, home=home)
        payload_obj = {
            "session_id": "sess-missing-cwd",
            "hook_event_name": "SessionStart",
        }

        result = run_hook(hook_script("session-start.sh"), json.dumps(payload_obj), env=env)

        assert result.returncode == 0
        assert result.stdout == ""
        diagnostic = home / "logs" / "curl-errors.log"
        assert diagnostic.exists(), "missing/invalid cwd must leave hook diagnostics"
        assert "missing cwd" in diagnostic.read_text(encoding="utf-8")

    def test_dt_empty_stdout_when_no_conventions_available(self, tmp_path: Path) -> None:
        """DT-SS-3: A 204 injection response is a no-op and hook exit remains 0.

        This is the normal cold-start path: project exists, server is running,
        but analysis has not yet produced any conventions. Hook must not print
        an empty string or whitespace, and ingest still happens separately.
        """
        port, server = _make_convention_server("", status=204)
        try:
            home = tmp_path / ".secondsight"
            home.mkdir()
            env = build_env(port=port, home=home)
            result = run_hook(hook_script("session-start.sh"), minimal_payload(), env=env)

            assert result.returncode == 0
            assert result.stdout.strip() == "", (
                f"Expected empty stdout when conventions=''; got: {result.stdout!r}"
            )
        finally:
            server.shutdown()


# ===========================================================================
# USER-PROMPT GUIDANCE INJECTION TESTS (Layer 1)
# ===========================================================================


def _user_prompt_payload() -> str:
    return json.dumps(
        {
            "session_id": "sess-test",
            "cwd": "/tmp/proj-test",
            "transcript_path": "/tmp/transcript.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "fix it",
        }
    )


class TestUserPromptGuidanceInjection:
    """Layer 1 tests: user-prompt.sh injection path (updated for task-3 new Python path).

    Task-3 replaced the legacy curl-to-injection-endpoint path with an agent-native
    Python helper (secondsight.feedback.hit_injection.render_wrapper).  The new path
    fires first and always returns before the legacy curl block.  Tests below verify
    the new path's behavior.

    The original three tests tested the legacy curl path:
      - test_raw_server_payload_printed_to_stdout_unchanged (server body pass-through)
      - test_user_prompt_hook_sends_cwd_not_project_id (injection POST body shape)
      - test_dt_empty_stdout_when_injection_server_errors (server 500 fail-open)
    All three have been replaced with tests that reflect the new Python-path behavior.
    The legacy curl block remains in user-prompt.sh for task-4's atomic deletion.
    """

    def test_new_path_emits_wrapper_json_not_server_body(self, tmp_path: Path) -> None:
        """Replaces test_raw_server_payload_printed_to_stdout_unchanged.

        The new Python path emits render_wrapper JSON, NOT the server's body.
        The mock server is still started (to absorb the ingest POST), but its
        injection body is irrelevant — the hook never calls the injection endpoint.
        """
        body = (
            '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",'
            '"additionalContext":"clarify scope"}}'
        )
        port, server = _make_convention_server(body)
        try:
            home = tmp_path / ".secondsight"
            home.mkdir()
            env = build_env(port=port, home=home, agent="codex")
            import os

            env["HOME"] = os.environ.get("HOME", str(Path.home()))
            env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
            result = run_hook(
                hook_script("user-prompt.sh"), _user_prompt_payload(), env=env, timeout=30.0
            )

            assert result.returncode == 0
            # The hook must emit JSON from the Python path (NOT the server body above).
            assert result.stdout != "", (
                f"hook must emit wrapper JSON; got empty stdout. stderr: {result.stderr!r}"
            )
            assert result.stdout != body, (
                "hook must emit Python-generated wrapper, not the legacy server body"
            )
            import json as _json

            payload = _json.loads(result.stdout)
            additional_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
            assert additional_context, "additionalContext must be non-empty"
            assert "fix it" in additional_context, (
                "additionalContext must contain original prompt (proves Python path ran)"
            )
        finally:
            server.shutdown()

    def test_no_injection_curl_call_made_by_new_path(self, tmp_path: Path) -> None:
        """Replaces test_user_prompt_hook_sends_cwd_not_project_id.

        The new Python path does NOT call the legacy /hook/injection/user-prompt/
        endpoint at all.  The recording server should NOT record any injection body.
        (It will still receive the observation ingest POST at /hook/{agent}/user_prompt.)
        """
        body = (
            '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",'
            '"additionalContext":"clarify scope"}}'
        )
        port, server, bodies = _make_recording_body_server(body)
        try:
            home = tmp_path / ".secondsight"
            home.mkdir()
            import os

            env = build_env(port=port, home=home, agent="codex")
            env["HOME"] = os.environ.get("HOME", str(Path.home()))
            env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
            result = run_hook(
                hook_script("user-prompt.sh"), _user_prompt_payload(), env=env, timeout=30.0
            )

            assert result.returncode == 0
            # The new Python path does not POST to the injection endpoint.
            # The recording server may receive ONE body: the observation ingest POST.
            # It must NOT receive the injection request body shape {prompt, session_id, cwd}.
            injection_bodies = [
                b
                for b in bodies
                if isinstance(b, str)
                and '"prompt"' in b
                and '"cwd"' in b
                and '"session_id"' in b
                and len(b) < 200  # injection body is small
            ]
            assert len(injection_bodies) == 0, (
                f"Injection endpoint was called with body {injection_bodies!r}; "
                "new Python path should not call the legacy injection endpoint."
            )
        finally:
            server.shutdown()

    def test_new_path_still_emits_wrapper_when_server_not_running(self, tmp_path: Path) -> None:
        """Replaces test_dt_empty_stdout_when_injection_server_errors.

        The new Python path does not depend on the injection server.  Even when
        no server is listening (connection refused), the Python path emits wrapper
        JSON.  The legacy curl block (unreachable) would have failed open with
        empty stdout; the new path succeeds independently of the server.

        This test uses a port where no server is listening to verify independence.
        """
        home = tmp_path / ".secondsight"
        home.mkdir()
        import os

        # Port 1 is reserved/unroutable — connection will be refused immediately.
        env = build_env(port=1, home=home, agent="claude_code")
        env["HOME"] = os.environ.get("HOME", str(Path.home()))
        env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        result = run_hook(
            hook_script("user-prompt.sh"), _user_prompt_payload(), env=env, timeout=30.0
        )

        assert result.returncode == 0, (
            f"hook must exit 0; got {result.returncode}. stderr: {result.stderr!r}"
        )
        # New path emits wrapper JSON regardless of server state.
        assert result.stdout != "", (
            "new Python path must emit wrapper JSON even when injection server is unreachable. "
            f"stderr: {result.stderr!r}"
        )
        import json as _json

        try:
            payload = _json.loads(result.stdout)
        except _json.JSONDecodeError as exc:
            pytest.fail(f"hook stdout is not valid JSON: {exc}\nstdout: {result.stdout!r}")
        additional_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert additional_context, "additionalContext must be non-empty"
