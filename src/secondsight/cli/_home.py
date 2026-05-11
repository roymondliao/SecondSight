"""Shared home-directory / paths helpers for the CLI subcommands.

Centralises the precedence rules so every subcommand resolves paths the
same way:

    1. CLI flag (--home, --claude-home) if non-empty;
    2. environment variable (SECONDSIGHT_HOME, CLAUDE_HOME) if set;
    3. compiled-in default (~/.secondsight, ~/.claude).

Resolution is intentionally lazy (called per-command, not at import) so
that env vars set by a wrapping shell script — or by tests via
``monkeypatch.setenv`` — are honoured.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SECONDSIGHT_HOME = "~/.secondsight"
DEFAULT_CLAUDE_HOME = "~/.claude"


def secondsight_home(cli_value: str = "") -> Path:
    """Resolve SecondSight home with the standard CLI precedence chain."""
    chosen = cli_value or os.environ.get("SECONDSIGHT_HOME") or DEFAULT_SECONDSIGHT_HOME
    return Path(chosen).expanduser()


def claude_home(cli_value: str = "") -> Path:
    """Resolve the Claude Code config home (~/.claude by default)."""
    chosen = cli_value or os.environ.get("CLAUDE_HOME") or DEFAULT_CLAUDE_HOME
    return Path(chosen).expanduser()


__all__ = [
    "DEFAULT_CLAUDE_HOME",
    "DEFAULT_SECONDSIGHT_HOME",
    "claude_home",
    "secondsight_home",
]
