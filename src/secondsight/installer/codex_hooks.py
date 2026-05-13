"""CodexHooksPatcher — idempotent merge into ``~/.codex/hooks.json``.

Codex hook registration is stored in ``hooks.json`` under the agent home.
Observed local shape (verified 2026-05-13)::

    {
      "hooks": {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "..."}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "..."}]}],
        "UserPromptSubmit": [{...}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "..."}]}],
        "Stop": [{...}]
      }
    }

Like the Claude patcher, this module is:
    - idempotent;
    - non-destructive to user-defined hooks;
    - atomic on write;
    - loud on malformed JSON / wrong-typed ``hooks`` sections.

Silent failure surface this module closes:
    - hooks.json missing -> create empty dict, write fresh.
    - hooks.json malformed JSON -> raise ``InvalidSettingsError``; never
      silently overwrite user data.
    - hooks.json present but ``hooks`` key has wrong type -> raise
      ``InvalidSettingsError``.
    - SecondSight commands installed under a different path -> surfaced as
      ``conflict`` in ``PatchPlan.actions``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from secondsight.installer.claude_settings import (
    InvalidSettingsError,
    PatchPlan,
    SECONDSIGHT_MARKER,
)

EVENT_TYPE_TO_CODEX_HOOK: dict[str, str] = {
    "tool_use_start": "PreToolUse",
    "tool_use_end": "PostToolUse",
    "user_prompt": "UserPromptSubmit",
    "session_start": "SessionStart",
    "session_end": "Stop",
}


class CodexHooksPatcher:
    """Own reads, plans, and writes for ``~/.codex/hooks.json``."""

    def __init__(self, hooks_path: Path) -> None:
        self._hooks_path = Path(hooks_path)

    @property
    def hooks_path(self) -> Path:
        return self._hooks_path

    def plan(self, hook_dir: Path) -> PatchPlan:
        existing, file_existed = self._read_or_empty()
        hooks_section = existing.get("hooks", {})
        if hooks_section is None:
            hooks_section = {}
        if not isinstance(hooks_section, dict):
            raise InvalidSettingsError(
                f"{self._hooks_path}: top-level 'hooks' must be a JSON object, "
                f"got {type(hooks_section).__name__}"
            )

        actions: dict[str, str] = {}
        foreign: list[str] = []
        for event_type_value, codex_event in EVENT_TYPE_TO_CODEX_HOOK.items():
            target_entry = _build_entry(hook_dir, event_type_value)
            entries = hooks_section.get(codex_event, [])
            if not isinstance(entries, list):
                raise InvalidSettingsError(
                    f"{self._hooks_path}: hooks['{codex_event}'] must be a list, "
                    f"got {type(entries).__name__}"
                )
            match, foreign_paths = _classify_entries(entries, target_entry)
            foreign.extend(foreign_paths)
            if match == "exact":
                actions[codex_event] = "skip"
            elif match in {"different_path", "different_shape"}:
                actions[codex_event] = "conflict"
            else:
                actions[codex_event] = "add"

        return PatchPlan(
            settings_path=self._hooks_path,
            file_existed=file_existed,
            actions=dict(sorted(actions.items())),
            foreign_secondsight_paths=sorted(set(foreign)),
        )

    def apply(self, hook_dir: Path) -> PatchPlan:
        plan = self.plan(hook_dir)
        if not plan.has_changes:
            self._hooks_path.parent.mkdir(parents=True, exist_ok=True)
            return plan

        existing, _ = self._read_or_empty()
        hooks_section = existing.setdefault("hooks", {})

        for event_type_value, codex_event in EVENT_TYPE_TO_CODEX_HOOK.items():
            if plan.actions.get(codex_event) != "add":
                continue
            entries = hooks_section.setdefault(codex_event, [])
            entries.append(_build_entry(hook_dir, event_type_value))

        self._atomic_write(existing)
        return plan

    def _read_or_empty(self) -> tuple[dict[str, Any], bool]:
        if not self._hooks_path.exists():
            return {}, False
        try:
            raw = self._hooks_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InvalidSettingsError(f"{self._hooks_path}: cannot read: {exc}") from exc
        if not raw.strip():
            return {}, True
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidSettingsError(f"{self._hooks_path}: invalid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise InvalidSettingsError(
                f"{self._hooks_path}: top-level must be a JSON object, got {type(obj).__name__}"
            )
        return obj, True

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self._hooks_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_hooks_",
            suffix=".json",
            dir=str(self._hooks_path.parent),
        )
        needs_cleanup = True
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._hooks_path)
            needs_cleanup = False
        finally:
            if needs_cleanup and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def _event_type_to_script_name(event_type_value: str) -> str:
    return {
        "tool_use_start": "pre-tool-use.sh",
        "tool_use_end": "post-tool-use.sh",
        "user_prompt": "user-prompt.sh",
        "session_start": "session-start.sh",
        "session_end": "session-end.sh",
    }[event_type_value]


def _build_command(hook_dir: Path, event_type_value: str) -> str:
    script_name = _event_type_to_script_name(event_type_value)
    script_path = (hook_dir / script_name).expanduser()
    return (
        f"SECONDSIGHT_AGENT=codex {script_path} "
        f"{SECONDSIGHT_MARKER} agent=codex event={event_type_value}"
    )


def _matcher_for_event_type(event_type_value: str) -> str | None:
    if event_type_value in {"tool_use_start", "tool_use_end"}:
        return "*"
    return None


def _build_entry(hook_dir: Path, event_type_value: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "hooks": [
            {
                "type": "command",
                "command": _build_command(hook_dir, event_type_value),
            }
        ]
    }
    matcher = _matcher_for_event_type(event_type_value)
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _classify_entries(entries: list[Any], target_entry: dict[str, Any]) -> tuple[str, list[str]]:
    target_command = target_entry["hooks"][0]["command"]
    target_path = _strip_marker(target_command)
    expected_matcher = target_entry.get("matcher")
    foreign: list[str] = []
    has_exact = False
    has_shape_conflict = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for hook in inner:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command")
            if not isinstance(cmd, str):
                continue
            if SECONDSIGHT_MARKER not in cmd:
                continue
            stripped = _strip_marker(cmd)
            if stripped != target_path:
                foreign.append(cmd)
                continue

            entry_matcher = entry.get("matcher") if "matcher" in entry else None
            if entry_matcher == expected_matcher:
                has_exact = True
            else:
                has_shape_conflict = True
                foreign.append(cmd)
    if has_exact:
        return "exact", foreign
    if has_shape_conflict:
        return "different_shape", foreign
    if foreign:
        return "different_path", foreign
    return "absent", foreign


def _strip_marker(command: str) -> str:
    idx = command.find(SECONDSIGHT_MARKER)
    if idx < 0:
        return command.strip()
    return command[:idx].strip()


__all__ = [
    "CodexHooksPatcher",
    "EVENT_TYPE_TO_CODEX_HOOK",
]
