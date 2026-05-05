"""Death + unit tests for ClaudeSettingsPatcher (GUR-98 / P1-11).

Death tests (must go RED before production code; documented even when the
production path now passes them so future refactors stay honest):

  DT-1  Existing user hooks (no SecondSight marker) MUST be preserved
        verbatim across plan + apply. A regression that overwrote the
        whole hooks section would silently delete the user's setup.
  DT-2  apply() is idempotent: re-running with the same hook_dir yields
        the same on-disk bytes. A regression that appended a duplicate
        SecondSight entry on every run would slowly fill settings.json.
  DT-3  Malformed settings.json (invalid JSON) must raise
        InvalidSettingsError; never silently overwrite user data.
  DT-4  hooks key with the wrong type (list, str) must raise — refusing
        to coerce structure on someone else's file.
  DT-5  Foreign SecondSight install (different path) is detected as
        "conflict" and surfaces in PatchPlan.foreign_secondsight_paths.
  DT-6  Atomic write: a crash mid-write does not leave a half-written
        settings.json. Asserted by checking that no .tmp_settings_*.json
        remains in the parent dir after an exception.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from secondsight.installer.claude_settings import (
    SECONDSIGHT_MARKER,
    ClaudeSettingsPatcher,
    InvalidSettingsError,
    find_existing_secondsight_paths,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hook_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hooks"
    d.mkdir()
    return d


def _settings(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


# ---------------------------------------------------------------------------
# DT-1: existing user hooks preserved across plan + apply
# ---------------------------------------------------------------------------


def test_death_existing_user_hooks_preserved(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/bin/my-other-hook.sh",
                                }
                            ],
                        }
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "echo stopping"}]}],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    patcher = ClaudeSettingsPatcher(settings_path)
    patcher.apply(_hook_dir(tmp_path))

    written = json.loads(settings_path.read_text(encoding="utf-8"))
    pre = written["hooks"]["PreToolUse"]
    # The user's existing entry is untouched...
    assert any(
        e.get("hooks", [{}])[0].get("command") == "/usr/bin/my-other-hook.sh" for e in pre
    ), f"user's existing PreToolUse entry must be preserved, got {pre!r}"
    # ...AND a SecondSight entry was added alongside it.
    assert any(SECONDSIGHT_MARKER in e.get("hooks", [{}])[0].get("command", "") for e in pre), (
        "SecondSight entry should be appended to PreToolUse list"
    )
    # The Stop event the user defined is still present, unchanged.
    assert written["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo stopping"


# ---------------------------------------------------------------------------
# DT-2: apply() is idempotent
# ---------------------------------------------------------------------------


def test_death_apply_is_idempotent(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    hook_dir = _hook_dir(tmp_path)

    patcher = ClaudeSettingsPatcher(settings_path)
    plan_first = patcher.apply(hook_dir)
    bytes_first = settings_path.read_bytes()

    plan_second = patcher.apply(hook_dir)
    bytes_second = settings_path.read_bytes()

    assert bytes_first == bytes_second, (
        "apply() must be byte-idempotent; second call should not re-add entries"
    )
    assert plan_first.has_changes is True
    assert plan_second.has_changes is False
    assert all(action == "skip" for action in plan_second.actions.values()), (
        f"second apply() should skip every event, got {plan_second.actions!r}"
    )


# ---------------------------------------------------------------------------
# DT-3 + DT-4: malformed input is rejected, never silently coerced
# ---------------------------------------------------------------------------


def test_death_malformed_json_raises(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    settings_path.write_text("{ this is not valid json", encoding="utf-8")
    patcher = ClaudeSettingsPatcher(settings_path)
    with pytest.raises(InvalidSettingsError):
        patcher.plan(_hook_dir(tmp_path))


def test_death_hooks_section_wrong_type_raises(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    settings_path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
    patcher = ClaudeSettingsPatcher(settings_path)
    with pytest.raises(InvalidSettingsError):
        patcher.plan(_hook_dir(tmp_path))


def test_death_event_entries_wrong_type_raises(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    settings_path.write_text(
        json.dumps({"hooks": {"PreToolUse": "not-a-list"}}),
        encoding="utf-8",
    )
    patcher = ClaudeSettingsPatcher(settings_path)
    with pytest.raises(InvalidSettingsError):
        patcher.plan(_hook_dir(tmp_path))


# ---------------------------------------------------------------------------
# DT-5: foreign SecondSight install is detected
# ---------------------------------------------------------------------------


def test_death_foreign_secondsight_install_detected(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    foreign = (
        f"/opt/other-secondsight/hooks/pre-tool-use.sh {SECONDSIGHT_MARKER} event=tool_use_start"
    )
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": foreign}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    patcher = ClaudeSettingsPatcher(settings_path)
    plan = patcher.plan(_hook_dir(tmp_path))

    assert plan.actions["PreToolUse"] == "conflict", (
        f"PreToolUse should be flagged as conflict, got {plan.actions!r}"
    )
    assert foreign in plan.foreign_secondsight_paths, (
        f"foreign install path must surface to caller, got {plan.foreign_secondsight_paths!r}"
    )


# ---------------------------------------------------------------------------
# DT-6: atomic-write — failure does not leave a half-written settings.json
# ---------------------------------------------------------------------------


def test_death_atomic_write_no_partial_file_on_crash(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    settings_path.write_text("{}", encoding="utf-8")
    patcher = ClaudeSettingsPatcher(settings_path)

    real_replace = os.replace

    def fail_once(src: str, dst: str) -> None:
        raise OSError("simulated rename failure")

    with patch("secondsight.installer.claude_settings.os.replace", side_effect=fail_once):
        with pytest.raises(OSError, match="simulated rename failure"):
            patcher.apply(_hook_dir(tmp_path))

    # Original file is still parseable as JSON (was never overwritten).
    obj = json.loads(settings_path.read_text(encoding="utf-8"))
    assert obj == {}, f"original settings.json must be untouched, got {obj!r}"

    # No tmp file leaked.
    leftovers = list(settings_path.parent.glob(".tmp_settings_*.json"))
    assert leftovers == [], f"tmp files leaked after failed apply: {leftovers!r}"

    # Sanity: real os.replace would have worked.
    assert real_replace is os.replace


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_apply_creates_fresh_settings_when_missing(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    assert not settings_path.exists()
    patcher = ClaudeSettingsPatcher(settings_path)
    plan = patcher.apply(_hook_dir(tmp_path))
    assert plan.file_existed is False
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "hooks" in written
    # All five SecondSight events registered.
    for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd"):
        assert event in written["hooks"], f"missing event {event}"


def test_dry_run_plan_does_not_touch_disk(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    patcher = ClaudeSettingsPatcher(settings_path)
    plan = patcher.plan(_hook_dir(tmp_path))
    assert plan.has_changes is True
    assert not settings_path.exists(), "plan() must not write the file"


def test_pre_and_post_tool_use_get_matcher(tmp_path: Path) -> None:
    settings_path = _settings(tmp_path)
    patcher = ClaudeSettingsPatcher(settings_path)
    patcher.apply(_hook_dir(tmp_path))
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert written["hooks"]["PreToolUse"][0].get("matcher") == "*"
    assert written["hooks"]["PostToolUse"][0].get("matcher") == "*"
    # SessionStart/End and UserPromptSubmit do NOT carry matcher (Claude Code
    # rejects matcher on non-tool events).
    assert "matcher" not in written["hooks"]["SessionStart"][0]


def test_find_existing_secondsight_paths_handles_missing_file(tmp_path: Path) -> None:
    assert find_existing_secondsight_paths(tmp_path / "nope.json") == []


def test_find_existing_secondsight_paths_handles_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    # Informational helper — must NOT raise on bad input.
    assert find_existing_secondsight_paths(p) == []
