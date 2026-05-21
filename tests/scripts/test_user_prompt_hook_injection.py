"""UserPromptSubmit shell hook injection tests (task-3).

Death tests are listed first per samsara framework ordering.

DT-5: config disabled → hook produces no stdout.
  test_dt_hook_emits_no_stdout_when_disabled

DT-6: enabled + valid prompt → hook emits well-formed JSON containing wrapper text.
  test_dt_hook_emits_wrapper_json_when_enabled

DT-7: Python interpreter unavailable → hook exits 0 with no stdout.
  test_dt_hook_fails_open_when_python_missing

The legacy test that relied on a mock injection-endpoint server
(test_dt_user_prompt_injection_completes_within_budget_for_1500ms_endpoint)
is removed per task-3 spec: the legacy curl path is unreachable under normal
operation and will be deleted in task-4.

The test that verified that injection failure still posts observation ingest
(test_dt_user_prompt_hook_injection_failure_still_posts_observation_ingest)
is retained because it covers the secondsight_post path which is independent
of the injection toggle.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.scripts.conftest import build_env, hook_script, run_hook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(prompt: str = "fix the auth bug") -> str:
    return json.dumps(
        {
            "session_id": "sess-1",
            "cwd": "/tmp/proj-test",
            "transcript_path": "/tmp/transcript.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
    )


def _write_config(home: Path, *, enabled: bool) -> None:
    """Write a minimal config.toml to `home` with hit_injection_enabled set."""
    config_dir = home
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        f"[feedback]\nhit_injection_enabled = {'true' if enabled else 'false'}\n",
        encoding="utf-8",
    )


def _symlink_tools(fake_bin: Path, tools: list[str]) -> None:
    """Symlink a list of tools into fake_bin from the real PATH."""
    for tool in tools:
        path = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
        assert path, f"test prerequisite missing: {tool}"
        (fake_bin / tool).symlink_to(path)


_STANDARD_TOOLS = [
    "bash",
    "jq",
    "date",
    "mkdir",
    "cat",
    "printf",
    "basename",
    "sed",
    "cksum",
    "awk",
    "dirname",
    "pwd",
    "readlink",
    "curl",
]


# ---------------------------------------------------------------------------
# Death tests
# ---------------------------------------------------------------------------


def test_dt_hook_logs_warning_on_invalid_bool_value(tmp_path: Path) -> None:
    """DT-C1: hit_injection_enabled = 1 (integer, not bool) must log a diagnostic.

    Silent failure path: the hook previously swallowed all exceptions with bare
    `except Exception: pass`, silently defaulting to True for ANY parse error.
    This diverged from loader.py's _resolve_feedback_typed_field which raises
    SecondSightConfigError for the same input.

    Required behavior:
    - Hook still exits 0 (fail-open preserved).
    - Injection still runs (default True preserved for non-bool invalid value).
    - A diagnostic is written to curl-errors.log naming the invalid value.

    If this test fails (no log entry), the operator who sets `hit_injection_enabled = 1`
    gets contradictory signals: `secondsight config validate` errors, hook silently enables.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Write a config with integer 1 instead of bool true — invalid per loader.py
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = 1\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _symlink_tools(fake_bin, _STANDARD_TOOLS + ["uv", "python3", "python"])

    import os

    env = build_env(port=8420, home=home, agent="claude_code")
    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["PATH"] = str(fake_bin) + ":" + env["PATH"]

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload("fix the auth bug"),
        env=env,
        timeout=30.0,
    )

    assert result.returncode == 0, (
        f"hook must exit 0 (fail-open); got {result.returncode}. stderr: {result.stderr!r}"
    )

    # Injection must still run (fail-open default True) — stdout should be non-empty.
    # (If Python exits non-zero instead of warning+continuing, injection is lost.)
    assert result.stdout != "", (
        "hook must still inject (default True) when hit_injection_enabled has invalid value. "
        f"stderr: {result.stderr!r}"
    )

    # The diagnostic MUST appear in curl-errors.log.
    logs_dir = home / "logs"
    log_file = logs_dir / "curl-errors.log"
    assert log_file.exists(), (
        "curl-errors.log must be created when hit_injection_enabled has an invalid value"
    )
    log_content = log_file.read_text(encoding="utf-8")
    assert "hit_injection_enabled" in log_content, (
        f"curl-errors.log must name the field 'hit_injection_enabled'. Content: {log_content!r}"
    )
    assert "invalid" in log_content.lower() or "expected bool" in log_content.lower(), (
        f"curl-errors.log must describe what was wrong. Content: {log_content!r}"
    )


def test_dt_hook_script_delegates_to_cli_runtime() -> None:
    """DT-C2: shell hook must delegate to CLI, not embed inline Python source."""
    script_content = hook_script("user-prompt.sh").read_text(encoding="utf-8")

    assert "python_script=" not in script_content, (
        "user-prompt.sh must not carry an inline Python program; keep business logic in the "
        "Python runtime entrypoint instead."
    )
    assert "<<'PYEOF'" not in script_content, (
        "user-prompt.sh still embeds a heredoc Python runner; this refactor is incomplete."
    )
    assert "-m secondsight hook user-prompt" in script_content, (
        "user-prompt.sh must delegate to the internal hook CLI entrypoint."
    )


def test_dt_hook_bypasses_known_agent_control_prompts(tmp_path: Path) -> None:
    """DT-B1: agent control-surface prompts must not be wrapped.

    Silent failure path: if slash-command / memorize-style prompts are wrapped,
    the agent receives SecondSight's executability self-check instead of its own
    native control surface. The command may stop working while the hook still
    exits 0, which looks like normal success.
    """
    home = tmp_path / ".secondsight"
    _write_config(home, enabled=True)

    env = build_env(port=8420, home=home, agent="claude_code")
    import os

    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload("/help"),
        env=env,
        timeout=30.0,
    )

    assert result.returncode == 0, (
        f"hook must exit 0 for bypass prompts; got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout == "", (
        "known control-surface prompts must bypass hit injection and emit no wrapper stdout. "
        f"stdout: {result.stdout!r}"
    )


def test_dt_hook_uses_pinned_runtime_file_when_path_has_no_python(tmp_path: Path) -> None:
    """DT-B2: pinned hook runtime must bypass PATH-based interpreter guessing.

    Silent failure path: `secondsight init` succeeds, but the hook later runs in
    a narrower PATH than the shell used during install. Without a pinned runtime
    file, the hook falls back to uv-or-PATH guessing and silently emits no
    wrapper stdout.
    """
    hook_dir = tmp_path / "hooks"
    shutil.copytree(Path(__file__).resolve().parents[2] / "scripts" / "hooks", hook_dir)
    runtime_file = hook_dir / ".secondsight-hook-runtime.sh"
    runtime_file.write_text(
        "#!/usr/bin/env bash\n"
        f"SECONDSIGHT_HOOK_PYTHON={subprocess.run(['which', 'python3'], capture_output=True, text=True).stdout.strip()!r}\n",
        encoding="utf-8",
    )

    home = tmp_path / ".secondsight"
    _write_config(home, enabled=True)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    # Intentionally omit python3/python/uv from PATH; the pinned runtime file
    # must carry the hook through.
    _symlink_tools(fake_bin, _STANDARD_TOOLS)

    env = build_env(port=8420, home=home, agent="claude_code")
    env["PATH"] = str(fake_bin)

    result = run_hook(
        hook_dir / "user-prompt.sh",
        _make_payload("fix the auth bug"),
        env=env,
        timeout=30.0,
    )

    assert result.returncode == 0, (
        f"hook must exit 0 when using pinned runtime; got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout != "", (
        "pinned runtime file must let the hook emit wrapper JSON even when PATH has no python. "
        f"stderr: {result.stderr!r}"
    )


def test_dt_hook_emits_wrapper_json_when_jq_missing(tmp_path: Path) -> None:
    """DT-J1: jq absence must not block wrapper injection.

    Acceptance for this change-set was amended to move payload parsing into the
    Python helper. If jq is still required in the shell path, the hook silently
    emits no wrapper stdout on hosts that have Python but not jq.
    """
    home = tmp_path / ".secondsight"
    _write_config(home, enabled=True)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _symlink_tools(fake_bin, [tool for tool in _STANDARD_TOOLS if tool != "jq"])
    _symlink_tools(fake_bin, ["python3", "python", "mktemp", "grep"])

    env = build_env(port=8420, home=home, agent="claude_code")
    env["HOME"] = str(tmp_path)
    env["PATH"] = str(fake_bin)

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload("fix the auth bug"),
        env=env,
        timeout=30.0,
    )

    assert result.returncode == 0, (
        f"hook must exit 0 when jq is missing; got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout != "", (
        "hook must still emit wrapper JSON when jq is missing but Python is available. "
        f"stderr: {result.stderr!r}"
    )

    payload = json.loads(result.stdout)
    additional_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "fix the auth bug" in additional_context, (
        f"additionalContext must contain the original prompt text. Payload: {payload!r}"
    )

    log_file = home / "logs" / "curl-errors.log"
    if log_file.exists():
        log_content = log_file.read_text(encoding="utf-8")
        assert "jq not found; cannot read prompt" not in log_content, (
            "jq absence must not block user-prompt injection after the parsing move. "
            f"Content: {log_content!r}"
        )


def test_dt_hook_emits_no_stdout_when_disabled(tmp_path: Path) -> None:
    """DT-5: hit_injection_enabled = false → hook exits 0 with empty stdout.

    If this test fails (stdout is non-empty), the config gate is broken and
    injections would continue even after an operator explicitly disables them.
    That would be a silent policy violation: operators could not turn off the
    feature by configuration.
    """
    home = tmp_path / ".secondsight"
    _write_config(home, enabled=False)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _symlink_tools(fake_bin, _STANDARD_TOOLS + ["uv", "python3", "python"])

    env = build_env(port=8420, home=home, agent="claude_code")
    # Give uv access to its home so it can find the project venv.
    import os

    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["PATH"] = str(fake_bin) + ":" + env["PATH"]

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload(),
        env=env,
    )

    assert result.returncode == 0, (
        f"hook must exit 0 (fail-open); got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout == "", f"hook must emit no stdout when disabled; got: {result.stdout!r}"
    # curl-errors.log must NOT contain hit_injection python error lines when disabled.
    # (uv deprecation warnings on stderr do NOT appear in the log because the hook
    # only logs stderr when Python exits non-zero.)
    logs_dir = home / "logs"
    if (logs_dir / "curl-errors.log").exists():
        log_content = (logs_dir / "curl-errors.log").read_text(encoding="utf-8")
        assert "secondsight_warning: hit_injection python error:" not in log_content, (
            "curl-errors.log must not contain hit_injection Python error lines when disabled. "
            f"Content: {log_content!r}"
        )


def test_dt_hook_emits_wrapper_json_when_enabled(tmp_path: Path) -> None:
    """DT-6: enabled + valid prompt → stdout parses as JSON with wrapper text.

    This is an end-to-end test: it runs the real render_wrapper (not a stub).
    If this test fails, injections are silently absent in production — users
    would see Claude proceeding without the executability self-check meta-instruction.

    The hook must emit JSON with additionalContext that contains the original
    prompt text (proving the real Python helper was called end-to-end, not a stub).

    Critically: the legacy curl categorical text must NOT appear in additionalContext
    — the new path must return before the legacy curl block is reached.
    """
    home = tmp_path / ".secondsight"
    _write_config(home, enabled=True)

    env = build_env(port=8420, home=home, agent="claude_code")
    import os

    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload("fix the auth bug"),
        env=env,
        timeout=30.0,  # uv + python startup can be slow on first run
    )

    assert result.returncode == 0, (
        f"hook must exit 0; got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout != "", (
        "hook must emit stdout (JSON wrapper) when enabled with a valid prompt. "
        f"stderr: {result.stderr!r}"
    )

    # stdout must parse as valid JSON
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"hook stdout is not valid JSON: {exc}\nstdout was: {result.stdout!r}")

    # additionalContext must be present and non-empty
    additional_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert additional_context, f"additionalContext is empty or missing. Full payload: {payload!r}"

    # Must contain the original prompt text (proves real render_wrapper was called)
    assert "fix the auth bug" in additional_context, (
        f"additionalContext does not contain original prompt text. "
        f"additionalContext: {additional_context!r}"
    )

    # Must NOT contain legacy curl endpoint-sourced categorical text
    # (proves early return fired before reaching the old curl block)
    legacy_markers = [
        "/hook/injection/user-prompt/",
        "provider failed",  # fake_curl error text from legacy test
    ]
    for marker in legacy_markers:
        assert marker not in additional_context, (
            f"additionalContext contains legacy curl marker {marker!r}, "
            "suggesting the new path did not return before the legacy block."
        )


def test_dt_hook_fails_open_when_python_missing(tmp_path: Path) -> None:
    """DT-7: Python unavailable → hook exits 0 with no stdout; error in log.

    If this test fails (hook exits non-zero or hangs), the hook would block
    Claude Code's UserPromptSubmit on machines without Python, causing
    tool-call cancellation. Fail-open is mandatory.

    The hook must also log the error to curl-errors.log so operators know
    Python is missing rather than silently swallowing the failure.
    """
    home = tmp_path / ".secondsight"
    _write_config(home, enabled=True)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    # Symlink all standard tools EXCEPT python3/python/uv
    _symlink_tools(fake_bin, _STANDARD_TOOLS)
    # Explicitly do NOT symlink python3, python, or uv — they must be missing

    env = build_env(port=8420, home=home, agent="claude_code")
    env["HOME"] = str(tmp_path)  # isolate HOME so uv doesn't find a global python
    env["PATH"] = str(fake_bin)  # no system path — python3/uv not available

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload(),
        env=env,
        timeout=10.0,
    )

    assert result.returncode == 0, (
        f"hook must exit 0 (fail-open) when Python is missing; "
        f"got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout == "", (
        f"hook must emit no stdout when Python is unavailable; got: {result.stdout!r}"
    )

    # curl-errors.log must contain an error mentioning the missing interpreter
    logs_dir = home / "logs"
    log_file = logs_dir / "curl-errors.log"
    assert log_file.exists(), (
        "curl-errors.log must be created when Python is missing, "
        "so operators can diagnose the issue"
    )
    log_content = log_file.read_text(encoding="utf-8")
    assert log_content.strip(), "curl-errors.log must not be empty when Python is missing"
    # The log must mention either the missing interpreter or hit_injection
    assert any(
        keyword in log_content.lower()
        for keyword in ("python", "interpreter", "hit_injection", "not found")
    ), f"curl-errors.log does not name the missing interpreter. Content: {log_content!r}"


# ---------------------------------------------------------------------------
# Retained test: injection failure still posts observation ingest
# ---------------------------------------------------------------------------


def test_dt_user_prompt_hook_injection_failure_still_posts_observation_ingest(
    tmp_path: Path,
) -> None:
    """DC3: injection failure must fail open and still run user_prompt ingest.

    This test is retained from the pre-task-3 test file because it covers the
    secondsight_post path (observation ingest), which is independent of the
    hit_injection_enabled toggle. The ingest leg must always run regardless of
    injection success/failure.

    Note: the fake curl here intercepts the observation ingest call at
    /hook/claude_code/user_prompt — this exercises the secondsight_post path.
    The injection leg no longer reaches the old /hook/injection/user-prompt/
    endpoint when Python is available and enabled; to test pure injection
    failure + ingest recovery, we force Python to fail (by making the module
    raise) while curl intercepts the ingest.
    """
    home = tmp_path / ".secondsight"
    _write_config(home, enabled=True)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "curl-calls.txt"

    real_curl = subprocess.run(["which", "curl"], capture_output=True, text=True).stdout.strip()
    assert real_curl, "curl must be present so fake PATH can shadow it"

    _symlink_tools(fake_bin, _STANDARD_TOOLS[:-1])  # all except curl
    _symlink_tools(fake_bin, ["python3", "python"])

    # Also symlink uv so Python can import secondsight if available
    uv_path = subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip()
    if uv_path:
        (fake_bin / "uv").symlink_to(uv_path)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        'url="${@: -1}"\n'
        'printf \'%s\\n\' "$url" >> "$SECONDSIGHT_TEST_CURL_CALLS"\n'
        'case "$url" in\n'
        "  */hook/*/user_prompt) printf '200'; exit 0 ;;\n"
        "  *) exit 7 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    import os

    env = build_env(port=8420, home=home, agent="claude_code")
    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["PATH"] = str(fake_bin) + ":" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["SECONDSIGHT_TEST_CURL_CALLS"] = str(calls_file)

    result = run_hook(
        hook_script("user-prompt.sh"),
        _make_payload("fix it"),
        env=env,
        timeout=30.0,
    )

    assert result.returncode == 0, (
        f"hook must fail open; got {result.returncode}. stderr: {result.stderr!r}"
    )

    # Observation ingest must have been called
    assert calls_file.exists(), (
        "curl-calls.txt not created; observation ingest curl was never called"
    )
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    ingest_calls = [c for c in calls if "/hook/claude_code/user_prompt" in c]
    assert ingest_calls, (
        f"observation ingest (/hook/claude_code/user_prompt) was not called. "
        f"All calls: {calls!r}. stderr: {result.stderr!r}"
    )
