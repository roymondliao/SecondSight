"""UserPromptSubmit injection route contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from secondsight.api.server import create_app
from secondsight.feedback.prompt_evaluator import PromptEvaluation


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# DEATH TESTS
# ===========================================================================


def test_dt_user_prompt_bypass_skips_evaluator_and_returns_204(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Bypass prompts must short-circuit before ambiguity evaluation."""
    from secondsight.api import injection

    called = False

    async def fake_evaluate_user_prompt(**_kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(decision="intervene", primary_category="missing_scope")

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={
                "project_id": "proj-1",
                "prompt": "/compact now",
                "session_id": "sess-1",
            },
        )

    assert response.status_code == 204
    assert response.content == b""
    assert called is False


def test_dt_user_prompt_evaluator_failure_fails_open_204(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Evaluator provider failure must not surface as a blocking hook error."""
    from secondsight.api import injection

    async def fake_evaluate_user_prompt(**_kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/codex",
            json={"project_id": "proj-1", "prompt": "fix it", "session_id": None},
        )

    assert response.status_code == 204
    assert response.content == b""


def test_dt_user_prompt_degraded_evaluator_logs_fail_open_reason(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """Malformed evaluator output still returns 204, but must leave evidence."""
    from secondsight.api import injection

    async def fake_evaluate_user_prompt(**_kwargs):
        return PromptEvaluation.pass_open(reason="malformed_output")

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/codex",
            json={"project_id": "proj-1", "prompt": "fix it", "session_id": None},
        )

    assert response.status_code == 204
    assert "UserPrompt evaluator failed open" in caplog.text
    assert "malformed_output" in caplog.text


def test_dt_user_prompt_cli_evaluator_forces_hook_disable_at_api_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """DC2: route-level CLI evaluation must spawn with hooks disabled."""
    from secondsight.feedback.prompt_evaluator import asyncio as evaluator_asyncio

    captured_env: dict[str, str] | None = None

    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "result": json.dumps({"decision": "pass", "primary_category": None}),
                }
            ).encode(),
            b"",
        )
    )

    async def fake_create_subprocess_exec(*_args, **kwargs):
        nonlocal captured_env
        captured_env = kwargs["env"]
        return proc

    monkeypatch.setattr(
        evaluator_asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setenv("SECONDSIGHT_DISABLE_HOOKS", "0")
    monkeypatch.setenv("SECONDSIGHT_PORT", "8420")

    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[general]\n"
        'mode = "cli"\n\n'
        "[analysis]\n"
        "timeout_seconds = 1\n\n"
        "[analysis.cli]\n"
        'default_agent = "claude_code"\n',
        encoding="utf-8",
    )
    cwd = tmp_path / "project-root"
    cwd.mkdir()

    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={
                "project_id": "proj-1",
                "prompt": "fix this",
                "session_id": "sess-1",
                "cwd": str(cwd),
            },
        )

    assert response.status_code == 204
    assert captured_env is not None
    assert captured_env.get("SECONDSIGHT_DISABLE_HOOKS") == "1"
    leaked = {
        key: value
        for key, value in captured_env.items()
        if key.startswith("SECONDSIGHT_") and key != "SECONDSIGHT_DISABLE_HOOKS"
    }
    assert leaked == {}


# ===========================================================================
# UNIT / CONTRACT TESTS
# ===========================================================================


def test_user_prompt_semantic_hit_returns_event_scoped_guidance_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A semantic hit maps to the fixed template and adapter event envelope."""
    from secondsight.api import injection

    async def fake_evaluate_user_prompt(**_kwargs):
        return SimpleNamespace(decision="intervene", primary_category="missing_scope")

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/codex",
            json={
                "project_id": "proj-1",
                "prompt": "fix this",
                "session_id": "sess-1",
            },
        )

    assert response.status_code == 200
    assert json.loads(response.text) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "Clarify the intended scope before acting, such as analysis only, "
                "code changes, tests, or refactoring."
            ),
        }
    }
    assert "systemMessage" not in response.text


def test_user_prompt_pass_decision_returns_204(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from secondsight.api import injection

    async def fake_evaluate_user_prompt(**_kwargs):
        return SimpleNamespace(decision="pass", primary_category=None)

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={"project_id": "proj-1", "prompt": "fix tests", "session_id": "sess-1"},
        )

    assert response.status_code == 204
    assert response.content == b""


def test_user_prompt_route_passes_hook_cwd_to_evaluator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from secondsight.api import injection

    captured_project_root: Path | None = None

    async def fake_evaluate_user_prompt(**kwargs):
        nonlocal captured_project_root
        captured_project_root = kwargs["project_root"]
        return SimpleNamespace(decision="pass", primary_category=None, failure_reason=None)

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    cwd = tmp_path / "project-root"
    cwd.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={
                "project_id": "proj-1",
                "prompt": "fix tests",
                "session_id": "sess-1",
                "cwd": str(cwd),
            },
        )

    assert response.status_code == 204
    assert captured_project_root == cwd


def test_dt_user_prompt_route_derives_project_id_from_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Hook injection must not require shell-side project_id derivation."""
    from secondsight.api import injection

    real_load_project_config = injection.load_project_config
    captured_project_id: str | None = None

    def capture_load_project_config(home: Path, project_id: str):
        nonlocal captured_project_id
        captured_project_id = project_id
        return real_load_project_config(home, project_id)

    async def fake_evaluate_user_prompt(**_kwargs):
        return SimpleNamespace(decision="pass", primary_category=None, failure_reason=None)

    monkeypatch.setattr(injection, "load_project_config", capture_load_project_config)
    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    cwd = tmp_path / "Project With Spaces"
    cwd.mkdir()

    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={
                "prompt": "fix tests",
                "session_id": "sess-1",
                "cwd": str(cwd),
            },
        )

    assert response.status_code == 204
    assert captured_project_id == "Project-With-Spaces"


def test_user_prompt_route_rejects_relative_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from secondsight.api import injection

    async def fake_evaluate_user_prompt(**_kwargs):
        raise AssertionError("relative cwd should fail before evaluation")

    monkeypatch.setattr(injection, "evaluate_user_prompt", fake_evaluate_user_prompt, raising=False)

    home = tmp_path / ".secondsight"
    home.mkdir()
    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/claude_code",
            json={
                "project_id": "proj-1",
                "prompt": "fix tests",
                "session_id": "sess-1",
                "cwd": "relative/path",
            },
        )

    assert response.status_code == 422
    assert "cwd" in response.text
