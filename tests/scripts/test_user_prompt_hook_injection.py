"""UserPromptSubmit shell hook injection + ingest ordering tests."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
from typing import Iterator

import pytest

from tests.scripts.conftest import build_env, hook_script, run_hook


def _user_prompt_payload() -> str:
    return json.dumps(
        {
            "session_id": "sess-1",
            "cwd": "/tmp/proj-test",
            "transcript_path": "/tmp/transcript.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "fix it",
        }
    )


def test_dt_user_prompt_hook_injection_failure_still_posts_observation_ingest(
    tmp_path: Path,
) -> None:
    """DC3: injection failure must fail open and still run user_prompt ingest."""
    home = tmp_path / ".secondsight"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "curl-calls.txt"
    data_file = tmp_path / "curl-data.jsonl"

    real_curl = subprocess.run(["which", "curl"], capture_output=True, text=True).stdout.strip()
    assert real_curl, "curl must be present so fake PATH can shadow it deterministically"
    for tool in (
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
    ):
        path = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
        assert path, f"test prerequisite missing: {tool}"
        (fake_bin / tool).symlink_to(path)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "data=''\n"
        "prev=''\n"
        'for arg in "$@"; do\n'
        '  if [ "$prev" = \'--data-raw\' ] || [ "$prev" = \'--data\' ]; then data="$arg"; fi\n'
        '  prev="$arg"\n'
        "done\n"
        'url="${@: -1}"\n'
        'printf \'%s\\n\' "$url" >> "$SECONDSIGHT_TEST_CURL_CALLS"\n'
        'printf \'%s\\n\' "$data" >> "$SECONDSIGHT_TEST_CURL_DATA"\n'
        'case "$url" in\n'
        "  */hook/injection/user-prompt/*) printf 'provider failed' >&2; exit 22 ;;\n"
        "  */hook/*/user_prompt) printf '200'; exit 0 ;;\n"
        "  *) exit 7 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    env = build_env(port=8420, home=home, agent="claude_code")
    env["PATH"] = str(fake_bin)
    env["SECONDSIGHT_TEST_CURL_CALLS"] = str(calls_file)
    env["SECONDSIGHT_TEST_CURL_DATA"] = str(data_file)

    result = run_hook(
        hook_script("user-prompt.sh"),
        _user_prompt_payload(),
        env=env,
    )

    assert result.returncode == 0
    paths = [
        url.removeprefix("http://127.0.0.1:8420")
        for url in calls_file.read_text(encoding="utf-8").splitlines()
    ]
    assert paths == [
        "/hook/injection/user-prompt/claude_code",
        "/hook/claude_code/user_prompt",
    ]
    injection_body = json.loads(data_file.read_text(encoding="utf-8").splitlines()[0])
    assert injection_body["cwd"] == "/tmp/proj-test"


# ---------------------------------------------------------------------------
# Death test: hook latency budget must accommodate evaluator subprocess
#
# Background (bugfix/2026-05-20_user-prompt-injection-timeout):
#   scripts/hooks/user-prompt.sh ran curl with --max-time 0.5, copy-pasted
#   from session-start.sh. The UserPrompt injection endpoint dispatches
#   evaluate_user_prompt which, in cli mode, spawns the configured agent
#   CLI subprocess — a seconds-level operation. 500ms was guaranteed to
#   time out, producing 0% successful injections in production.
#
# This test pins the hook budget against a simulated 1500ms evaluator
# response. A passing test means the hook script's --max-time exceeds
# realistic evaluator latency.
# ---------------------------------------------------------------------------


class _DelayedInjectionHandler(BaseHTTPRequestHandler):
    """HTTP handler that delays /hook/injection/user-prompt/* responses.

    All other paths (notably the observation ingest at
    /hook/{agent}/user_prompt) respond immediately so the second curl call
    in user-prompt.sh is unaffected — the death test isolates the injection
    latency budget.
    """

    _injection_delay_s: float = 1.5
    _injection_body: bytes = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "death-test-guidance",
            }
        }
    ).encode()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)
        if self.path.startswith("/hook/injection/user-prompt/"):
            time.sleep(self._injection_delay_s)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self._injection_body)))
            self.end_headers()
            self.wfile.write(self._injection_body)
            return
        # observation ingest and any other path: immediate 200
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002,D102
        pass


@pytest.fixture()
def slow_injection_server() -> Iterator[int]:
    """HTTP server that delays injection responses by 1.5s. Yields port."""
    server = HTTPServer(("127.0.0.1", 0), _DelayedInjectionHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()


def test_dt_user_prompt_injection_completes_within_budget_for_1500ms_endpoint(
    tmp_path: Path,
    slow_injection_server: int,
) -> None:
    """Death test for bugfix/2026-05-20_user-prompt-injection-timeout.

    The injection endpoint in cli mode takes ~1.5s (real evaluator spawns
    a CLI subprocess). The hook's curl budget must exceed that, or every
    injection silently times out.

    Currently fails (pre-fix) because scripts/hooks/user-prompt.sh uses
    --max-time 0.5. Passes once the budget is raised to comfortably exceed
    1.5s of realistic evaluator latency.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()
    env = build_env(port=slow_injection_server, home=home, agent="claude_code")

    result = run_hook(
        hook_script("user-prompt.sh"),
        _user_prompt_payload(),
        env=env,
        timeout=10.0,
    )

    assert result.returncode == 0, (
        "hook must fail open regardless; this assertion guards fail-open contract"
    )
    assert "death-test-guidance" in result.stdout, (
        "hook --max-time is below the evaluator's realistic 1.5s response time, "
        "so the injection payload never reaches stdout. Increase --max-time in "
        "scripts/hooks/user-prompt.sh injection curl call."
    )
