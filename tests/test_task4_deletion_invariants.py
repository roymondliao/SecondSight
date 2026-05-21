"""Death tests for task-4: atomic deletion of the sidecar classifier pathway.

These tests must run RED before any deletion (because the references still exist
and the endpoint still serves), then GREEN after deletion (because the deleted
symbols are gone and the endpoint returns 404).

DT-8: No dangling references to deleted sidecar symbols after deletion.
DT-9: Deleted endpoint returns 404 (not 5xx — route does not exist, not broken handler).
DT-10: Hook script post-deletion still produces wrapper output for valid prompts.
DT-11: curl-errors.log does not gain new injection-endpoint timeout entries.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from secondsight.api.server import create_app


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
HOOKS_DIR = REPO_ROOT / "scripts" / "hooks"

# DT-8 production-surface scope: src/ and scripts/ only.
# Tests intentionally reference the deleted symbol names in docstrings/assertion
# messages (documenting their absence + this very file's death-test patterns).
# Including tests/ in DT-8 would self-match and create a false-positive failure
# pattern that obscures real dangling references in production code.
_DELETED_SYMBOLS_PATTERN = "prompt_evaluator|/hook/injection/user-prompt"
_GREP_SCOPE = ["src/", "scripts/"]
_GREP_INCLUDE = ["--include=*.py", "--include=*.sh", "--include=*.toml"]


def _run_hook(
    script: Path,
    payload: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/env", "bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _user_prompt_payload(prompt: str = "fix the auth bug") -> str:
    return json.dumps(
        {
            "session_id": "sess-dt-test",
            "cwd": "/tmp/proj-test",
            "transcript_path": "/tmp/transcript.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
    )


def _build_env(home: Path, *, port: int = 8420) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "SECONDSIGHT_PORT": str(port),
        "SECONDSIGHT_HOME": str(home),
        "SECONDSIGHT_AGENT": "claude_code",
    }


# ---------------------------------------------------------------------------
# DT-8: No dangling references to deleted sidecar symbols
# ---------------------------------------------------------------------------


def test_dt_no_dangling_references_to_deleted_sidecar() -> None:
    """DT-8: grep src/ tests/ scripts/ for deleted symbols must return empty.

    Silent failure path: if this test passes BEFORE deletion, the sidecar was
    already removed before task-4 ran (unexpected). If this test fails AFTER
    deletion, a reference was missed — the "atomic" deletion was incomplete.

    This test is expected to FAIL (RED) before task-4's deletions because
    the references still exist. After deletion it must pass (GREEN).

    Grep scope corrected to include scripts/ per task-1 fix-iteration finding
    (the original scope omitted scripts/ and would have silently passed while
    scripts/hooks/user-prompt.sh:118 held a live curl reference to the deleted
    route).
    """
    result = subprocess.run(
        [
            "grep",
            "-rln",
            _DELETED_SYMBOLS_PATTERN,
            *_GREP_SCOPE,
            *_GREP_INCLUDE,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    matches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert matches == [], (
        f"Found {len(matches)} file(s) referencing deleted sidecar symbols. "
        f"Task-4's atomic deletion is incomplete.\n"
        f"Files with dangling references:\n" + "\n".join(f"  {m}" for m in matches)
    )


# ---------------------------------------------------------------------------
# DT-9: Deleted endpoint returns 404
# ---------------------------------------------------------------------------


def test_dt_deleted_endpoint_returns_404(tmp_path: Path) -> None:
    """DT-9: POST /hook/injection/user-prompt/claude_code must return 404.

    Silent failure path: if the route is still registered, the handler runs and
    may return 204 (pass-through) or 500 (import error). Either is worse than
    404 because the caller cannot distinguish "route gone by design" from
    "route broken by error". 404 is the correct signal: the route was
    intentionally removed.

    This test is expected to FAIL (RED) before task-4's deletions because the
    route still exists and returns 2xx or 422. After deletion it must pass
    (GREEN) because the route is not registered and FastAPI returns 404.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()
    app = create_app(secondsight_home=home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={"project_id": "proj-1", "prompt": "fix it", "session_id": "s1"},
        )
    assert response.status_code == 404, (
        f"Expected 404 (route deleted); got {response.status_code}. "
        f"Body: {response.text!r}. "
        "If 2xx/422: route is still registered. If 5xx: route was deleted but "
        "a broken import or handler raised an unhandled exception."
    )


# ---------------------------------------------------------------------------
# DT-10: Hook still works after atomic deletion
# ---------------------------------------------------------------------------


def test_dt_hook_still_works_after_atomic_deletion(tmp_path: Path) -> None:
    """DT-10: hook with config enabled + valid stdin produces wrapper JSON on stdout.

    Silent failure path: the legacy curl block was removed but the new Python
    path was also accidentally damaged (e.g., removed too much from the hook
    script). The hook would exit 0 with empty stdout — no injection — but no
    error would surface. This test catches that silent regression.

    This test is expected to PASS both before AND after deletion (the new Python
    path is present in both states). A RED result at any point means the hook is
    broken regardless of the deletion state.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()
    # Write a config with hit_injection_enabled = true (explicit)
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = true\n",
        encoding="utf-8",
    )

    env = _build_env(home)
    result = _run_hook(
        HOOKS_DIR / "user-prompt.sh",
        _user_prompt_payload("fix the auth bug"),
        env=env,
        timeout=30.0,
    )

    assert result.returncode == 0, (
        f"Hook must exit 0 after deletion; got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert result.stdout != "", (
        "Hook must produce wrapper JSON on stdout after deletion. "
        "Empty stdout means the new Python path is broken or injection was silently skipped. "
        f"stderr: {result.stderr!r}"
    )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"Hook stdout is not valid JSON after deletion: {exc}\nstdout: {result.stdout!r}"
        )

    additional_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert additional_context, (
        f"additionalContext must be present and non-empty after deletion. Full payload: {payload!r}"
    )
    assert "fix the auth bug" in additional_context, (
        "additionalContext must contain the original prompt text (proves Python path ran). "
        f"additionalContext: {additional_context!r}"
    )


# ---------------------------------------------------------------------------
# DT-11: curl-errors.log does not gain injection-endpoint timeout entries
# ---------------------------------------------------------------------------


def test_dt_curl_errors_log_does_not_gain_injection_entries_after_deletion(
    tmp_path: Path,
) -> None:
    """DT-11: 10 hook invocations must leave no injection-endpoint entries in curl-errors.log.

    Silent failure path: the legacy curl block is still in the hook (not fully
    deleted), or a new code path added after task-4 calls the old endpoint URL.
    The hook's `|| return 0` swallows the curl error exit, so stdout looks fine
    while the log fills with injection-endpoint connection errors. This test
    detects that rot before it accumulates.

    Pattern checked: any line containing "/hook/injection/user-prompt/" in the log.
    This pattern matches both the curl error message (which includes the URL) and
    any direct log writes referencing the deleted route.

    This test is expected to FAIL (RED) before task-4's deletions IF the legacy
    curl block is exercised (it is currently unreachable behind the `return 0`,
    so it may pass even before deletion). After deletion, it must pass (GREEN)
    because the curl block is gone and can never produce such log entries.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = true\n",
        encoding="utf-8",
    )
    logs_dir = home / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "curl-errors.log"
    # Start with a clean log file
    log_file.write_text("", encoding="utf-8")

    env = _build_env(home, port=1)  # dead port — forces any curl call to fail

    # Run the hook 10 times with diverse prompts
    prompts = [
        "fix the auth bug",
        "add unit tests",
        "refactor the database layer",
        "what does this function do?",
        "help me understand the codebase",
        "implement the new feature from the spec",
        "debug this failing test",
        "review my changes",
        "how do I deploy this?",
        "clean up the dead code",
    ]
    for i, prompt in enumerate(prompts):
        result = _run_hook(
            HOOKS_DIR / "user-prompt.sh",
            _user_prompt_payload(prompt),
            env=env,
            timeout=30.0,
        )
        assert result.returncode == 0, (
            f"Hook must exit 0 even with dead port; "
            f"invocation {i} got {result.returncode}. stderr: {result.stderr!r}"
        )

    # Check log for injection-endpoint entries
    if log_file.exists():
        log_content = log_file.read_text(encoding="utf-8")
        injection_entries = [
            line for line in log_content.splitlines() if "/hook/injection/user-prompt/" in line
        ]
        assert injection_entries == [], (
            f"Found {len(injection_entries)} injection-endpoint entry(ies) in curl-errors.log "
            f"after {len(prompts)} hook invocations. The legacy curl block may still be active.\n"
            f"Entries:\n" + "\n".join(f"  {e}" for e in injection_entries)
        )
