"""UserPromptSubmit shell hook injection + ingest ordering tests."""

import json
from pathlib import Path
import subprocess

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
