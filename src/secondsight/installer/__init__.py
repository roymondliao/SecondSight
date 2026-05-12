"""SecondSight installer — `secondsight init` machinery (GUR-98 / P1-11).

Two collaborators:
  * :class:`HookInstaller`        — copies bundled hook scripts to a target dir.
  * :class:`ClaudeSettingsPatcher` — idempotently merges SecondSight hook entries
    into a Claude Code ``settings.json`` without disturbing any pre-existing
    user-defined hooks.
  * :class:`CodexHooksPatcher` — idempotently merges SecondSight hook entries
    into a Codex ``hooks.json`` without disturbing any pre-existing user-defined
    hooks.

The CLI command lives in ``secondsight.cli.init``; this package is the
boundary-free, side-effect-isolated core that the CLI orchestrates.
"""

from __future__ import annotations

from secondsight.installer.claude_settings import (
    SECONDSIGHT_MARKER,
    ClaudeSettingsPatcher,
    PatchPlan,
)
from secondsight.installer.codex_hooks import (
    CodexHooksPatcher,
)
from secondsight.installer.hook_install import (
    HOOK_FILES,
    HookInstaller,
    HookInstallPlan,
    bundled_hook_dir,
)

__all__ = [
    "HOOK_FILES",
    "SECONDSIGHT_MARKER",
    "ClaudeSettingsPatcher",
    "CodexHooksPatcher",
    "HookInstallPlan",
    "HookInstaller",
    "PatchPlan",
    "bundled_hook_dir",
]
