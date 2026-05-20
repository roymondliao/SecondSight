"""Death + unit tests for CodexHooksPatcher.

Death tests:
  DT-1  Existing user hooks are preserved across apply().
  DT-2  apply() is idempotent and does not duplicate SecondSight entries.
  DT-3  Malformed hooks.json raises InvalidSettingsError.
  DT-4  Wrong-typed hooks section raises InvalidSettingsError.
  DT-5  Foreign SecondSight install path is surfaced as a conflict.
"""

from __future__ import annotations

from typing import cast

import json
from pathlib import Path

import pytest

from secondsight.installer.claude_settings import (
    InvalidSettingsError,
    SECONDSIGHT_MARKER,
)
from secondsight.installer.codex_hooks import CodexHooksPatcher


def _hook_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hooks"
    d.mkdir()
    return d


def _hooks_json(tmp_path: Path) -> Path:
    return tmp_path / "hooks.json"


def _secondsight_entry(entries: list[dict[str, object]]) -> dict[str, object]:
    for entry in entries:
        hooks = entry.get("hooks")
        if not isinstance(hooks, list) or not hooks:
            continue
        first = hooks[0]
        if not isinstance(first, dict):
            continue
        command = cast(dict[str, object], first).get("command")
        if isinstance(command, str) and SECONDSIGHT_MARKER in command:
            return entry
    raise AssertionError(f"missing SecondSight entry in {entries!r}")


def test_death_existing_user_hooks_preserved(tmp_path: Path) -> None:
    hooks_path = _hooks_json(tmp_path)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/bin/my-codex-hook.sh",
                                }
                            ]
                        }
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    patcher = CodexHooksPatcher(hooks_path)
    patcher.apply(_hook_dir(tmp_path))

    written = json.loads(hooks_path.read_text(encoding="utf-8"))
    session_start = written["hooks"]["SessionStart"]
    assert any(
        entry.get("hooks", [{}])[0].get("command") == "/usr/bin/my-codex-hook.sh"
        for entry in session_start
    ), f"user SessionStart hook must be preserved, got {session_start!r}"
    assert any(
        SECONDSIGHT_MARKER in entry.get("hooks", [{}])[0].get("command", "")
        for entry in session_start
    ), "SecondSight entry should be added alongside existing SessionStart hooks"
    assert written["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo stop"


def test_death_apply_is_idempotent(tmp_path: Path) -> None:
    hooks_path = _hooks_json(tmp_path)
    patcher = CodexHooksPatcher(hooks_path)
    hook_dir = _hook_dir(tmp_path)

    plan_first = patcher.apply(hook_dir)
    bytes_first = hooks_path.read_bytes()

    plan_second = patcher.apply(hook_dir)
    bytes_second = hooks_path.read_bytes()

    assert bytes_first == bytes_second, "second apply() must not append duplicate entries"
    assert plan_first.has_changes is True
    assert plan_second.has_changes is False
    assert all(action == "skip" for action in plan_second.actions.values()), (
        f"second apply() should skip every event, got {plan_second.actions!r}"
    )


def test_death_malformed_json_raises(tmp_path: Path) -> None:
    hooks_path = _hooks_json(tmp_path)
    hooks_path.write_text("{ broken", encoding="utf-8")
    patcher = CodexHooksPatcher(hooks_path)
    with pytest.raises(InvalidSettingsError):
        patcher.plan(_hook_dir(tmp_path))


def test_death_hooks_section_wrong_type_raises(tmp_path: Path) -> None:
    hooks_path = _hooks_json(tmp_path)
    hooks_path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
    patcher = CodexHooksPatcher(hooks_path)
    with pytest.raises(InvalidSettingsError):
        patcher.plan(_hook_dir(tmp_path))


def test_death_foreign_secondsight_install_detected(tmp_path: Path) -> None:
    hooks_path = _hooks_json(tmp_path)
    foreign = (
        "SECONDSIGHT_AGENT=codex /opt/other-secondsight/hooks/session-start.sh "
        f"{SECONDSIGHT_MARKER} agent=codex event=session_start"
    )
    hooks_path.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": foreign}]}]}}
        ),
        encoding="utf-8",
    )

    patcher = CodexHooksPatcher(hooks_path)
    plan = patcher.plan(_hook_dir(tmp_path))

    assert plan.actions["SessionStart"] == "conflict", (
        f"SessionStart should be flagged as conflict, got {plan.actions!r}"
    )
    assert foreign in plan.foreign_secondsight_paths


def test_apply_creates_fresh_hooks_json_when_missing(tmp_path: Path) -> None:
    hooks_path = _hooks_json(tmp_path)
    patcher = CodexHooksPatcher(hooks_path)
    plan = patcher.apply(_hook_dir(tmp_path))

    assert plan.file_existed is False
    written = json.loads(hooks_path.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "PostToolUse", "SessionStart", "Stop", "UserPromptSubmit"):
        assert event in written["hooks"], f"missing event {event}"
    for event in ("PreToolUse", "PostToolUse"):
        entry = _secondsight_entry(written["hooks"][event])
        assert entry.get("matcher") == "*", f"{event} must install matcher-aware hook entry"
    for event in ("SessionStart", "Stop", "UserPromptSubmit"):
        entry = _secondsight_entry(written["hooks"][event])
        assert "matcher" not in entry, f"{event} should not install matcher"


def test_session_start_and_user_prompt_install_codex_agent_transport(
    tmp_path: Path,
) -> None:
    hooks_path = _hooks_json(tmp_path)
    hook_dir = _hook_dir(tmp_path)
    patcher = CodexHooksPatcher(hooks_path)
    patcher.apply(hook_dir)

    written = json.loads(hooks_path.read_text(encoding="utf-8"))
    session_entry = _secondsight_entry(written["hooks"]["SessionStart"])
    prompt_entry = _secondsight_entry(written["hooks"]["UserPromptSubmit"])
    session_hooks = cast(list[dict[str, object]], session_entry["hooks"])
    prompt_hooks = cast(list[dict[str, object]], prompt_entry["hooks"])
    session_command = session_hooks[0]["command"]
    prompt_command = prompt_hooks[0]["command"]

    assert isinstance(session_command, str)
    assert isinstance(prompt_command, str)
    assert session_command.startswith("SECONDSIGHT_AGENT=codex ")
    assert prompt_command.startswith("SECONDSIGHT_AGENT=codex ")
    assert f"{hook_dir / 'session-start.sh'}" in session_command
    assert f"{hook_dir / 'user-prompt.sh'}" in prompt_command
    assert "event=session_start" in session_command
    assert "event=user_prompt" in prompt_command


def test_death_tool_hook_without_matcher_is_conflict(tmp_path: Path) -> None:
    hook_dir = _hook_dir(tmp_path)
    hooks_path = _hooks_json(tmp_path)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "SECONDSIGHT_AGENT=codex "
                                        f"{hook_dir / 'pre-tool-use.sh'} "
                                        f"{SECONDSIGHT_MARKER} agent=codex event=tool_use_start"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    patcher = CodexHooksPatcher(hooks_path)
    plan = patcher.plan(hook_dir)

    assert plan.actions["PreToolUse"] == "conflict", (
        f"tool hook without matcher must be treated as conflict, got {plan.actions!r}"
    )
