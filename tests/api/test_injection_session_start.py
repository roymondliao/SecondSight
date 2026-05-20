"""Contract tests for the dedicated injection namespace.

Death cases:
- feedback config must resolve from [feedback], not a hard-coded selector default.
- /hook/injection/session-start/{agent} must return the raw hook stdout payload.
- no selection output means 204, not an obsolete JSON envelope.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from secondsight.analysis.schemas import Directive, DirectiveStatus, DirectiveType
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import create_app
from secondsight.config.loader import load_project_config
from secondsight.storage.directives_repository import DirectivesRepository

UTC = timezone.utc
NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
EXPECTED_SINGLE_CONVENTION_TEXT = (
    "SecondSight project conventions:\n"
    "These are project-derived behavioral guidelines for this session. "
    "Follow them unless the user explicitly gives conflicting instructions.\n\n"
    "- Always inspect the current diff before editing"
)


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def _seed_convention(
    home: Path,
    *,
    project_id: str,
    directive_id: str,
    instruction: str,
    frequency: float,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
) -> None:
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    repo = DirectivesRepository(resources.db_engine)
    repo.create_schema()
    repo.insert(
        Directive(
            id=directive_id,
            project_id=project_id,
            type=DirectiveType.CONVENTION,
            status=status,
            instruction=instruction,
            frequency=frequency,
            identity_key=f"key-{directive_id}",
            source_sessions=["s1"],
            source_flag_type="unnecessary_read",
            created_at=NOW,
            updated_at=NOW,
            disabled_at=NOW if status == DirectiveStatus.DISABLED else None,
            disabled_reason="test disable" if status == DirectiveStatus.DISABLED else None,
        )
    )
    asyncio.run(registry.aclose())


def test_dt_feedback_config_resolves_non_default_budget_from_project_layer(
    tmp_path: Path,
) -> None:
    """DC4: a project [feedback] budget must not silently fall back to 2000."""
    home = tmp_path / ".secondsight"
    project_dir = home / "projects" / "proj-1"
    project_dir.mkdir(parents=True)
    (home / "config.toml").write_text(
        "[feedback]\nconvention_injection_budget = 1999\nconvention_top_n = 11\n",
        encoding="utf-8",
    )
    (project_dir / "config.toml").write_text(
        "[feedback]\nconvention_injection_budget = 321\nconvention_top_n = 4\n",
        encoding="utf-8",
    )

    cfg = load_project_config(home, "proj-1")

    assert cfg.feedback.convention_injection_budget == 321
    assert cfg.feedback.convention_top_n == 4


def test_dt_session_start_injection_selects_conventions_and_renders_agent_payload(
    tmp_path: Path,
) -> None:
    """DC1: selected conventions must become the target agent SessionStart envelope."""
    home = tmp_path / ".secondsight"
    home.mkdir()
    _seed_convention(
        home,
        project_id="proj-1",
        directive_id="d1",
        instruction="Always inspect the current diff before editing",
        frequency=0.9,
    )

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/claude_code",
            json={"project_id": "proj-1"},
        )

    assert response.status_code == 200
    assert json.loads(response.text) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": EXPECTED_SINGLE_CONVENTION_TEXT,
        }
    }


def test_dt_session_start_injection_no_conventions_returns_204(
    tmp_path: Path,
) -> None:
    """Empty convention selection is true no-op payload semantics."""
    home = tmp_path / ".secondsight"
    home.mkdir()

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/codex",
            json={"project_id": "empty-proj"},
        )

    assert response.status_code == 204
    assert response.content == b""


def test_dt_session_start_non_default_feedback_budget_changes_selection_runtime(
    tmp_path: Path,
) -> None:
    """DC4: runtime selection must use resolved [feedback], not hard-coded 2000."""
    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nconvention_injection_budget = 10\n",
        encoding="utf-8",
    )
    _seed_convention(
        home,
        project_id="proj-1",
        directive_id="large",
        instruction="L" * 80,
        frequency=0.99,
    )
    _seed_convention(
        home,
        project_id="proj-1",
        directive_id="small",
        instruction="S" * 16,
        frequency=0.5,
    )

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/codex",
            json={"project_id": "proj-1"},
        )

    assert response.status_code == 200
    payload = json.loads(response.text)
    assert payload == {
        "systemMessage": (
            "SecondSight project conventions:\n"
            "These are project-derived behavioral guidelines for this session. "
            "Follow them unless the user explicitly gives conflicting instructions.\n\n"
            "- " + ("S" * 16)
        )
    }
    assert "L" * 80 not in response.text


def test_session_start_convention_template_wraps_selected_items() -> None:
    """Unit: selected convention lines are framed before adapter output rendering."""
    from secondsight.api.injection import _render_session_start_convention_template

    assert _render_session_start_convention_template(["- A", "- B"]) == (
        "SecondSight project conventions:\n"
        "These are project-derived behavioral guidelines for this session. "
        "Follow them unless the user explicitly gives conflicting instructions.\n\n"
        "- A\n"
        "- B"
    )


def test_session_start_convention_template_empty_lines_return_none() -> None:
    """Unit: empty formatted selections become 204 upstream, not blank payloads."""
    from secondsight.api.injection import _render_session_start_convention_template

    assert _render_session_start_convention_template([]) is None
    assert _render_session_start_convention_template(["", "   "]) is None


def test_dt_session_start_injection_returns_raw_adapter_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """DC1: the new route returns raw rendered body, never the old envelope."""
    from secondsight.api import injection

    async def fake_text(*, project_id: str, feedback_config, **_) -> str:
        assert project_id == "proj-1"
        assert feedback_config.convention_injection_budget == 123
        return "Injected convention text"

    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nconvention_injection_budget = 123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(injection, "_build_session_start_text", fake_text)

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/codex",
            json={"project_id": "proj-1"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.text == '{"systemMessage":"Injected convention text"}'
    assert "conventions" not in response.text
    assert "budget_total" not in response.text
    assert json.loads(response.text) == {"systemMessage": "Injected convention text"}


def test_session_start_injection_empty_text_returns_204(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """No selected conventions is a no-op hook output, not an empty envelope."""
    from secondsight.api import injection

    async def fake_text(*, project_id: str, feedback_config, **_) -> str | None:
        assert project_id == "empty-proj"
        return None

    home = tmp_path / ".secondsight"
    home.mkdir()
    monkeypatch.setattr(injection, "_build_session_start_text", fake_text)

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/claude_code",
            json={"project_id": "empty-proj"},
        )

    assert response.status_code == 204
    assert response.content == b""


def test_session_start_injection_invalid_feedback_config_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Bad [feedback] config is an operator error, not valid empty selection."""
    from secondsight.api import injection

    async def fake_text(*, project_id: str, feedback_config, **_) -> str:
        raise AssertionError("invalid config should fail before selection text assembly")

    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nconvention_injection_budget = 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(injection, "_build_session_start_text", fake_text)

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/codex",
            json={"project_id": "proj-1"},
        )

    assert response.status_code == 500
    assert "config" in response.text.lower()


def test_session_start_injection_adapter_render_failure_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Broken adapter render seams must not look like no-op injection."""
    from secondsight.adapters.codex import CodexAdapter
    from secondsight.api import injection

    async def fake_text(*, project_id: str, feedback_config, **_) -> str:
        return "text that should be rendered"

    def broken_render(self, text: str) -> str:
        raise RuntimeError("render exploded")

    home = tmp_path / ".secondsight"
    home.mkdir()
    monkeypatch.setattr(injection, "_build_session_start_text", fake_text)
    monkeypatch.setattr(CodexAdapter, "render_session_start_output", broken_render)

    with _client(home) as client:
        response = client.post(
            "/hook/injection/session-start/codex",
            json={"project_id": "proj-1"},
        )

    assert response.status_code == 500
    assert "render" in response.text.lower()


def test_user_prompt_injection_default_cli_auto_without_state_fails_open_204(
    tmp_path: Path,
) -> None:
    """Default CLI auto config without state fails open instead of blocking."""
    home = tmp_path / ".secondsight"
    home.mkdir()

    with _client(home) as client:
        response = client.post(
            "/hook/injection/user-prompt/codex",
            json={"project_id": "proj-1", "prompt": "ambiguous prompt", "session_id": "s1"},
        )

    assert response.status_code == 204
    assert response.content == b""
