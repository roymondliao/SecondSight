"""ClaudeSettingsPatcher — idempotent merge into ``~/.claude/settings.json``.

SD §3.9.2: Claude Code only fires hook scripts that are registered in its
``settings.json``. Copying a script to ``~/.claude/hooks/`` without registering
it is a silent failure mode (hook present on disk, no events ever generated).
This module owns the mutation contract.

Settings shape Claude Code expects (verified from a real install)::

    {
      "hooks": {
        "PreToolUse":  [{"matcher": "*", "hooks": [{"type": "command",
                                                    "command": "<absolute-path>"}]}],
        "PostToolUse": [{...}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "..."}]}],
        "SessionStart": [...],
        "SessionEnd":   [...]
      }
    }

Tool-use events use ``matcher`` (default ``"*"``); non-tool events
(SessionStart, SessionEnd, UserPromptSubmit) omit ``matcher``.

Idempotency contract:
    apply() is safe to call N times. The Nth call leaves settings.json
    byte-identical to the first call's result, given the same plan + the
    same target file (modulo unrelated user edits between calls).

Non-destructive merge contract:
    A user's existing entries (whose commands do NOT carry the SecondSight
    marker) are preserved verbatim. We only ADD a SecondSight-marked entry
    if no SecondSight-marked entry already exists for that event.

Silent failure surface this module closes:
    - settings.json missing -> create empty dict, write fresh.
    - settings.json malformed JSON -> raise ``InvalidSettingsError``; never
      silently overwrite user data.
    - settings.json present but ``hooks`` key has wrong type (list, str, ...)
      -> raise ``InvalidSettingsError``; refuse to patch.
    - SecondSight commands installed under a path that differs from the
      caller-supplied path -> ``find_existing_secondsight_paths`` surfaces them
      so the CLI can warn (double-install detection, item 2 of Step 0).
    - Atomic write: tmp file + os.replace so a crash mid-write never leaves
      a corrupt settings.json on disk.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Marker embedded in every SecondSight-installed hook command. Used both to
# detect existing SecondSight entries (idempotency) and to surface foreign
# SecondSight installs (double-install detection). Embedded as a comment-style
# token because Claude Code treats the command string as a shell command and
# would error on unrecognised syntax — so the marker must be valid in `sh -c`.
# We use a `:` no-op prefix so the marker is shell-safe even if the rest of the
# command is ever stripped by mistake.
SECONDSIGHT_MARKER = "# secondsight-hook"

# Mapping from canonical SecondSight EventType values to Claude Code's hook
# event names. Inverse of the table in adapters/claude_code.py — kept here to
# avoid importing the adapters layer just for the install path.
EVENT_TYPE_TO_CLAUDE_HOOK: dict[str, str] = {
    "tool_use_start": "PreToolUse",
    "tool_use_end": "PostToolUse",
    "user_prompt": "UserPromptSubmit",
    "session_start": "SessionStart",
    "session_end": "SessionEnd",
}

# Hook events that need a `matcher` field (tool events only).
_NEEDS_MATCHER: frozenset[str] = frozenset({"PreToolUse", "PostToolUse"})


class InvalidSettingsError(ValueError):
    """Raised when settings.json exists but is not the shape we can patch."""


@dataclass(frozen=True)
class PatchPlan:
    """What the patcher *would* do, surfaced for --dry-run.

    `actions` is one of: "add" (a new SecondSight entry), "skip" (a
    SecondSight entry for that event already exists at the same path), or
    "conflict" (a SecondSight entry exists but points at a *different*
    install path — double-install).
    """

    settings_path: Path
    file_existed: bool
    actions: dict[str, str] = field(default_factory=dict)
    foreign_secondsight_paths: list[str] = field(default_factory=list)
    """Paths that look like SecondSight commands but are NOT the install
    path the caller asked us to register. Empty list = no double install."""

    @property
    def has_changes(self) -> bool:
        return any(action == "add" for action in self.actions.values())


class ClaudeSettingsPatcher:
    """Owns reads, plans, and writes for ~/.claude/settings.json.

    The `apply()` method takes a hook-script directory and registers each
    of the five SecondSight hook scripts under the matching Claude Code
    event name. The patcher never touches non-SecondSight hooks.
    """

    def __init__(self, settings_path: Path) -> None:
        self._settings_path = Path(settings_path)

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, hook_dir: Path) -> PatchPlan:
        """Compute what apply() would do, without writing anything.

        Raises:
            InvalidSettingsError: if settings.json exists with a malformed
                ``hooks`` section. Never silently coerces.
        """
        existing, file_existed = self._read_or_empty()
        actions: dict[str, str] = {}
        foreign: list[str] = []

        hooks_section = existing.get("hooks", {})
        if hooks_section is None:
            hooks_section = {}
        if not isinstance(hooks_section, dict):
            raise InvalidSettingsError(
                f"{self._settings_path}: top-level 'hooks' must be a JSON object, "
                f"got {type(hooks_section).__name__}"
            )

        for event_type_value, claude_event in EVENT_TYPE_TO_CLAUDE_HOOK.items():
            target_command = self._build_command(hook_dir, event_type_value)
            entries = hooks_section.get(claude_event, [])
            if not isinstance(entries, list):
                raise InvalidSettingsError(
                    f"{self._settings_path}: hooks['{claude_event}'] must be a list, "
                    f"got {type(entries).__name__}"
                )
            ss_match, foreign_paths = _classify_entries(entries, target_command)
            foreign.extend(foreign_paths)
            if ss_match == "exact":
                actions[claude_event] = "skip"
            elif ss_match == "different_path":
                actions[claude_event] = "conflict"
            else:
                actions[claude_event] = "add"

        # Stable order for deterministic dry-run output.
        return PatchPlan(
            settings_path=self._settings_path,
            file_existed=file_existed,
            actions=dict(sorted(actions.items())),
            foreign_secondsight_paths=sorted(set(foreign)),
        )

    def apply(self, hook_dir: Path) -> PatchPlan:
        """Execute the plan against settings.json. Atomic on POSIX.

        Returns the plan that was applied (the post-mutation snapshot).
        Idempotent: re-running with the same hook_dir produces the same
        on-disk bytes (modulo unrelated user keys).
        """
        plan = self.plan(hook_dir)
        if not plan.has_changes:
            # Still create the parent dir so a fresh `secondsight init` on a
            # box without ~/.claude leaves a sane breadcrumb (no-op when the
            # dir exists). Important for testability.
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            return plan

        existing, _ = self._read_or_empty()
        hooks_section = existing.setdefault("hooks", {})

        for event_type_value, claude_event in EVENT_TYPE_TO_CLAUDE_HOOK.items():
            if plan.actions.get(claude_event) != "add":
                continue
            target_command = self._build_command(hook_dir, event_type_value)
            entries = hooks_section.setdefault(claude_event, [])
            entry: dict[str, Any] = {
                "hooks": [{"type": "command", "command": target_command}],
            }
            if claude_event in _NEEDS_MATCHER:
                # Place matcher first so the JSON output mirrors the example
                # users see in Claude Code docs. Cosmetic, not functional.
                entry = {"matcher": "*", **entry}
            entries.append(entry)

        self._atomic_write(existing)
        return plan

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_or_empty(self) -> tuple[dict[str, Any], bool]:
        if not self._settings_path.exists():
            return {}, False
        try:
            raw = self._settings_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InvalidSettingsError(
                f"{self._settings_path}: cannot read: {exc}"
            ) from exc
        if not raw.strip():
            # Empty file is treated as empty object — a common state right
            # after `touch settings.json`. Distinct from missing file.
            return {}, True
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidSettingsError(
                f"{self._settings_path}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise InvalidSettingsError(
                f"{self._settings_path}: top-level must be a JSON object, "
                f"got {type(obj).__name__}"
            )
        return obj, True

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        # tmp file in the SAME directory so os.replace is a single-fs rename
        # (POSIX atomic). Any crash mid-write leaves either the OLD file
        # intact or the NEW file fully written — never a half-written one.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_settings_",
            suffix=".json",
            dir=str(self._settings_path.parent),
        )
        needs_cleanup = True
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._settings_path)
            needs_cleanup = False
        finally:
            if needs_cleanup and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _build_command(hook_dir: Path, event_type_value: str) -> str:
        """Return the command-string we register in settings.json.

        Format::

            <absolute-script-path>  # secondsight-hook event=<event_type>

        The trailing comment is read by Claude Code's shell as a no-op (every
        POSIX shell drops `#` to end-of-line). It serves three purposes:
          * a marker that this entry is ours (idempotency);
          * a path-independent identity check (so renaming the install dir
            still flags "this used to be ours");
          * a human-readable hint when an operator opens settings.json.
        """
        script_name = _event_type_to_script_name(event_type_value)
        script_path = (hook_dir / script_name).expanduser()
        # We do NOT resolve() — operator may want to keep ~ in the path for
        # portability across machines that share a home dir mount. The
        # invariant we DO enforce is that hook_dir is a valid directory
        # the caller already validated.
        return f'{script_path} {SECONDSIGHT_MARKER} event={event_type_value}'


def find_existing_secondsight_paths(settings_path: Path) -> list[str]:
    """Return SecondSight-marked install paths found in settings.json.

    Used by the CLI to warn before patching if a different install path is
    already registered. Quietly returns [] for missing/empty/malformed files
    — this helper is informational only.
    """
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    found: list[str] = []
    hooks_section = obj.get("hooks", {})
    if not isinstance(hooks_section, dict):
        return []
    for entries in hooks_section.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for h in inner:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if isinstance(cmd, str) and SECONDSIGHT_MARKER in cmd:
                    found.append(cmd)
    return sorted(set(found))


def _event_type_to_script_name(event_type_value: str) -> str:
    """Map ``tool_use_start`` -> ``pre-tool-use.sh`` etc.

    The mapping is canonical — see scripts/hooks/* filenames. We do NOT use
    EVENT_TYPE_TO_CLAUDE_HOOK directly because Claude's hook event PascalCase
    name (`PreToolUse`) differs from the script filename (`pre-tool-use.sh`);
    both follow Claude Code's naming convention but the kebab-case-vs-Pascal
    mapping is one-way only.
    """
    return {
        "tool_use_start": "pre-tool-use.sh",
        "tool_use_end": "post-tool-use.sh",
        "user_prompt": "user-prompt.sh",
        "session_start": "session-start.sh",
        "session_end": "session-end.sh",
    }[event_type_value]


def _classify_entries(
    entries: list[Any],
    target_command: str,
) -> tuple[str, list[str]]:
    """Inspect existing entries; classify against our target command.

    Returns:
        ("exact", []) if a SecondSight entry already maps to *our* path.
        ("different_path", [foreign...]) if SecondSight entries exist but
            registered a different install path (double-install).
        ("absent", []) if no SecondSight entry exists for this event.
    """
    target_path = _strip_marker(target_command)
    foreign: list[str] = []
    has_exact = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            if not isinstance(cmd, str):
                continue
            if SECONDSIGHT_MARKER not in cmd:
                continue
            stripped = _strip_marker(cmd)
            if stripped == target_path:
                has_exact = True
            else:
                foreign.append(cmd)
    if has_exact:
        return "exact", foreign
    if foreign:
        return "different_path", foreign
    return "absent", foreign


def _strip_marker(command: str) -> str:
    """Return the shell-command portion of a SecondSight command line.

    Strips the trailing ``# secondsight-hook event=<...>`` so two install
    paths can be compared directly. The split is tolerant of extra spaces
    or trailing flags between the path and the marker.
    """
    idx = command.find(SECONDSIGHT_MARKER)
    if idx < 0:
        return command.strip()
    return command[:idx].strip()


__all__ = [
    "ClaudeSettingsPatcher",
    "EVENT_TYPE_TO_CLAUDE_HOOK",
    "InvalidSettingsError",
    "PatchPlan",
    "SECONDSIGHT_MARKER",
    "find_existing_secondsight_paths",
]
