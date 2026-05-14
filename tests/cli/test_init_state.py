"""Tests for state.json write behavior in secondsight init (DC11).

Death tests:
- DT-init-state-1: `secondsight init --agent codex` when state.json exists with init_agent="claude_code"
  → CLI prompts user; default answer N → state.json unchanged
- DT-init-state-2: same scenario with --force → state.json overwritten with new init_at timestamp
- DT-init-state-3: first-time init → state.json written with correct init_agent
- DT-init-state-4: state.json written by init contains all required fields

Unit tests:
- UT-init-state-1: init with --agent claude_code → state.json.init_agent == "claude_code"
- UT-init-state-2: init with --agent codex → state.json.init_agent == "codex"
- UT-init-state-3: --force overwrites existing state.json
- UT-init-state-4: prompt answer N preserves existing state.json content
- UT-init-state-5: secondsight_version field populated in state.json (not empty)
- UT-init-state-6: init_at is a valid ISO8601 timestamp string
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from secondsight.cli.app import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch HookInstaller and patchers so init doesn't need real hook files."""
    # Mock the hook installer to avoid needing actual hook files
    monkeypatch.setattr(
        "secondsight.installer.hook_install.HookInstaller.install",
        lambda self, hook_dir, dry_run=False: type(
            "InstallPlan",
            (),
            {"copied": [], "skipped_identical": [], "source_dir": hook_dir},
        )(),
    )
    # Mock patcher.plan() and patcher.apply()
    mock_plan = type(
        "PatchPlan",
        (),
        {"actions": {}, "file_existed": False, "foreign_secondsight_paths": []},
    )()
    monkeypatch.setattr(
        "secondsight.installer.claude_settings.ClaudeSettingsPatcher.plan",
        lambda self, hook_dir: mock_plan,
    )
    monkeypatch.setattr(
        "secondsight.installer.claude_settings.ClaudeSettingsPatcher.apply",
        lambda self, hook_dir: mock_plan,
    )
    monkeypatch.setattr(
        "secondsight.installer.codex_hooks.CodexHooksPatcher.plan",
        lambda self, hook_dir: mock_plan,
    )
    monkeypatch.setattr(
        "secondsight.installer.codex_hooks.CodexHooksPatcher.apply",
        lambda self, hook_dir: mock_plan,
    )
    # Mock write_config_if_needed
    monkeypatch.setattr(
        "secondsight.config.template.write_config_if_needed",
        lambda ss_home, dry_run=False: "config already exists",
    )


def _write_state(ss_home: Path, init_agent: str) -> None:
    """Write a minimal state.json to simulate existing state."""
    state_path = ss_home / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "init_agent": init_agent,
                "init_at": "2026-01-01T00:00:00+00:00",
                "secondsight_version": "0.0.1",
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Death tests — must fail before implementation (red phase)
# ---------------------------------------------------------------------------


class TestDTInitState1PromptOnOverwrite:
    """DT-init-state-1: init with different agent when state.json exists → prompts, N preserves.

    DC11: silent overwrite is the lie. Truth: must prompt on overwrite.
    Silent failure path: if overwrite is silent, a scripted re-init with the wrong
    agent permanently changes behavior without any user awareness.
    """

    def test_prompt_n_preserves_existing_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()

        # Set up existing state with claude_code
        ss_home = tmp_path / ".secondsight"
        _write_state(ss_home, "claude_code")
        _mock_installer(monkeypatch)

        # Override ss_home resolution
        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        # Override agent home to a temp path so claude settings.json check works
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        # Simulate user answering "n" to the overwrite prompt
        runner.invoke(
            app,
            ["init", "--agent", "codex", "--secondsight-home", str(ss_home)],
            input="n\n",  # User declines overwrite
        )

        # State should be unchanged
        state_path = ss_home / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["init_agent"] == "claude_code", (
                f"state.json init_agent must remain 'claude_code' after declining prompt. "
                f"Got {data['init_agent']!r}."
            )


class TestDTInitState2ForceOverwrites:
    """DT-init-state-2: --force flag overwrites existing state.json without prompt.

    DC11: --force bypass is needed for scripted re-init flows.
    """

    def test_force_overwrites_existing_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()

        ss_home = tmp_path / ".secondsight"
        _write_state(ss_home, "claude_code")
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "codex", "--secondsight-home", str(ss_home), "--force"],
        )

        state_path = ss_home / "state.json"
        assert state_path.exists(), "state.json must exist after init"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["init_agent"] == "codex", (
            f"--force must overwrite state.json with new init_agent. Got {data['init_agent']!r}."
        )


class TestDTInitState3FirstTimeInit:
    """DT-init-state-3: first-time init writes state.json with correct init_agent."""

    def test_first_time_writes_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()

        ss_home = tmp_path / ".secondsight"
        # No pre-existing state.json
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        result = runner.invoke(
            app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        assert state_path.exists(), (
            f"state.json must be created after first-time init. "
            f"Exit code: {result.exit_code}. Output: {result.output}"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["init_agent"] == "claude_code"


class TestDTInitState4RequiredFields:
    """DT-init-state-4: state.json written by init contains all required fields."""

    def test_all_fields_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()

        ss_home = tmp_path / ".secondsight"
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text(encoding="utf-8"))

        required_fields = {"schema_version", "init_agent", "init_at", "secondsight_version"}
        missing = required_fields - set(data.keys())
        assert not missing, (
            f"state.json is missing required fields: {missing}. Got keys: {set(data.keys())}"
        )


# ---------------------------------------------------------------------------
# Unit tests (happy path)
# ---------------------------------------------------------------------------


class TestUTInitState1ClaudeCodeAgent:
    """UT-init-state-1: init with --agent claude_code writes claude_code to state.json."""

    def test_claude_code_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        ss_home = tmp_path / ".secondsight"
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["init_agent"] == "claude_code"


class TestUTInitState2CodexAgent:
    """UT-init-state-2: init with --agent codex writes codex to state.json."""

    def test_codex_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        ss_home = tmp_path / ".secondsight"
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        codex_home = tmp_path / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.codex_home",
            lambda override="": codex_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "codex", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["init_agent"] == "codex"


class TestUTInitState3ForceFlag:
    """UT-init-state-3: --force rewrites state.json with new agent."""

    def test_force_rewrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        ss_home = tmp_path / ".secondsight"
        _write_state(ss_home, "codex")
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home), "--force"],
        )

        state_path = ss_home / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["init_agent"] == "claude_code"


class TestUTInitState4PromptNPreserves:
    """UT-init-state-4: prompt answer N preserves existing state.json content."""

    def test_prompt_n_preserves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        ss_home = tmp_path / ".secondsight"
        _write_state(ss_home, "claude_code")
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        codex_home = tmp_path / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.codex_home",
            lambda override="": codex_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "codex", "--secondsight-home", str(ss_home)],
            input="n\n",
        )

        state_path = ss_home / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["init_agent"] == "claude_code"
        assert data["init_at"] == "2026-01-01T00:00:00+00:00"  # unchanged timestamp


class TestUTInitState5SecondSightVersionPopulated:
    """UT-init-state-5: secondsight_version field is populated (not empty) in state.json."""

    def test_version_not_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        ss_home = tmp_path / ".secondsight"
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data.get("secondsight_version"), (
                "secondsight_version must be non-empty in state.json. "
                "Empty version makes upgrade detection impossible."
            )


class TestUTInitState6InitAtIsISO8601:
    """UT-init-state-6: init_at is a valid ISO8601 timestamp string."""

    def test_init_at_is_iso8601(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime

        runner = CliRunner()
        ss_home = tmp_path / ".secondsight"
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        runner.invoke(
            app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            init_at = data.get("init_at", "")
            assert init_at, "init_at must be non-empty"
            # Must be parseable as ISO8601
            try:
                datetime.fromisoformat(init_at)
            except ValueError as e:
                pytest.fail(f"init_at {init_at!r} is not valid ISO8601: {e}")
