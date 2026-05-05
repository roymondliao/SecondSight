"""`secondsight init` — install hook scripts + register them with Claude Code.

Two-stage operation:

    1. Copy bundled hook scripts into ``<claude-home>/hooks/`` (default
       ``~/.claude/hooks/``).
    2. Patch ``<claude-home>/settings.json`` to register each script under the
       matching Claude Code hook event (PreToolUse, PostToolUse,
       UserPromptSubmit, SessionStart, SessionEnd).

Both stages are idempotent (re-running yields the same on-disk state) and
non-destructive (existing user hooks are preserved).

CLI surface (per SD §9.1 — supports both human + agent personas):

    secondsight init                   # human-friendly Rich output, applies changes
    secondsight init --dry-run         # preview only, no writes
    secondsight init --format json     # machine-readable summary
    secondsight init --claude-home DIR # override target (tests + non-default setups)
    secondsight init --hook-source DIR # override hook bundle (tests)

Exit codes:
    0 on success (or clean dry-run);
    1 on user-actionable error (invalid settings.json, double install warned);
    2 on packaging error (hook bundle missing).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console

from secondsight.cli._home import claude_home as resolve_claude_home
from secondsight.installer import (
    ClaudeSettingsPatcher,
    HookInstaller,
)
from secondsight.installer.claude_settings import InvalidSettingsError
from secondsight.installer.hook_install import HookBundleNotFoundError

app = typer.Typer(name="init", help="Install SecondSight hook scripts into Claude Code.")
_console = Console()


@app.callback(invoke_without_command=True)
def init(
    ctx: typer.Context,
    claude_home: str = typer.Option(
        "",
        "--claude-home",
        help="Override the Claude Code home directory (default: $CLAUDE_HOME or ~/.claude).",
    ),
    hook_source: str = typer.Option(
        "",
        "--hook-source",
        help="Override the bundled hook directory (used by tests; auto-discovered otherwise).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would change without writing to disk.",
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: 'text' (default, Rich) or 'json' (agent-friendly).",
    ),
) -> None:
    """Install SecondSight hooks into Claude Code (idempotent)."""
    # Typer's callback fires on bare `secondsight init`. We do not want sub-
    # commands here; treat any subcommand invocation as an error so future
    # additions don't silently shadow the install behaviour.
    if ctx.invoked_subcommand is not None:  # pragma: no cover — no subcmds defined
        return

    target_claude_home = resolve_claude_home(claude_home)
    hook_dir = target_claude_home / "hooks"
    settings_path = target_claude_home / "settings.json"

    installer = HookInstaller(
        source_dir=Path(hook_source).expanduser() if hook_source else None,
    )
    patcher = ClaudeSettingsPatcher(settings_path=settings_path)

    # ----- Stage 1: copy hook scripts -----
    try:
        install_plan = installer.install(hook_dir, dry_run=dry_run)
    except HookBundleNotFoundError as exc:
        _emit_error(output_format, "hook_bundle_missing", str(exc))
        raise typer.Exit(code=2) from exc

    # ----- Stage 2: plan/apply settings.json patch -----
    try:
        if dry_run:
            patch_plan = patcher.plan(hook_dir)
        else:
            patch_plan = patcher.apply(hook_dir)
    except InvalidSettingsError as exc:
        _emit_error(output_format, "settings_invalid", str(exc))
        raise typer.Exit(code=1) from exc

    summary = {
        "dry_run": dry_run,
        "hook_dir": str(hook_dir),
        "settings_path": str(settings_path),
        "scripts_copied": install_plan.copied,
        "scripts_skipped_identical": install_plan.skipped_identical,
        "scripts_source": str(install_plan.source_dir),
        "settings_actions": patch_plan.actions,
        "settings_file_existed": patch_plan.file_existed,
        "foreign_secondsight_paths": patch_plan.foreign_secondsight_paths,
    }

    if output_format == "json":
        # Stable JSON output for agent consumers (SD §9.1).
        typer.echo(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _render_text(summary)

    # Surface double-install as a non-fatal warning. We do NOT abort because
    # the user may legitimately have multiple SecondSight installs (e.g.
    # virtualenv vs system) and want our entry added alongside.
    if patch_plan.foreign_secondsight_paths and output_format != "json":
        _console.print(
            "[yellow]warning:[/yellow] Other SecondSight install paths are "
            "already registered in settings.json — review before running "
            "Claude Code to avoid double-firing hooks:"
        )
        for cmd in patch_plan.foreign_secondsight_paths:
            _console.print(f"  - {cmd}")


def _emit_error(output_format: str, code: str, message: str) -> None:
    if output_format == "json":
        typer.echo(json.dumps({"error": code, "message": message}, indent=2))
    else:
        _console.print(f"[red]error[/red] ({code}): {message}")


def _render_text(summary: dict[str, object]) -> None:
    title = "[cyan]Dry run[/cyan]" if summary["dry_run"] else "[cyan]Installed[/cyan]"
    _console.print(title)
    _console.print(f"  hook dir:      {summary['hook_dir']}")
    _console.print(f"  settings.json: {summary['settings_path']}")
    _console.print(f"  source bundle: {summary['scripts_source']}")

    copied = summary["scripts_copied"]
    skipped = summary["scripts_skipped_identical"]
    if copied:
        verb = "would copy" if summary["dry_run"] else "copied"
        _console.print(f"  hooks {verb}: {', '.join(copied)}")
    if skipped:
        _console.print(f"  hooks unchanged: {', '.join(skipped)}")

    actions = summary["settings_actions"]
    if isinstance(actions, dict):
        adds = [k for k, v in actions.items() if v == "add"]
        skips = [k for k, v in actions.items() if v == "skip"]
        confs = [k for k, v in actions.items() if v == "conflict"]
        if adds:
            verb = "would register" if summary["dry_run"] else "registered"
            _console.print(f"  settings {verb}: {', '.join(sorted(adds))}")
        if skips:
            _console.print(f"  settings already-correct: {', '.join(sorted(skips))}")
        if confs:
            _console.print(
                f"  [yellow]settings conflict (different install path):[/yellow] "
                f"{', '.join(sorted(confs))}"
            )


# Re-export utilities for tests.
__all__ = ["app", "asdict"]
