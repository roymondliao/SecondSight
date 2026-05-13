"""`secondsight init` — install hook scripts + register them with an agent.

Four-stage operation (the pre-check exists *because* stages 1 and 2 are
not jointly transactional — see review-response in changes/...):

    0. Pre-check: parse the target agent's registration file. Aborts on malformed
       JSON or wrong-typed `hooks` section BEFORE any hook script is
       copied to disk.
    1. Copy bundled hook scripts into ``<agent-home>/hooks/``.
    2. Patch the agent's registration file to register each script under
       the matching hook events.
    3. Generate (or diff-check) ``~/.secondsight/config.toml`` with default
       settings. This stage never raises — parse errors are printed and ignored
       so hook install can succeed independently of config generation.

The pre-check closes a silent-failure mode where registration-file validation
would otherwise happen *after* hook scripts had already landed on disk
without registration. A race-window between pre-check and apply still
exists (another writer could corrupt the registration file in between); when
that happens we exit with the explicit ``settings_invalid_after_hook_copy``
error code so the operator knows to re-run init after fixing that file.

All stages are idempotent (re-running yields the same on-disk state) and
non-destructive (existing user hooks are preserved).

CLI surface (per SD §9.1 — supports both human + agent personas):

    secondsight init                         # default: Claude Code at ~/.claude
    secondsight init --agent codex          # Codex at ~/.codex
    secondsight init --dry-run              # preview only, no writes
    secondsight init --format json          # machine-readable summary
    secondsight init --agent-home DIR       # override target home for chosen agent
    secondsight init --claude-home DIR      # Claude-specific compatibility flag
    secondsight init --codex-home DIR       # Codex-specific flag
    secondsight init --hook-source DIR      # override hook bundle (tests)
    secondsight init --secondsight-home DIR # override ~/.secondsight location

Exit codes:
    0 on success (or clean dry-run);
    1 on user-actionable error (invalid registration file, double install warned);
    2 on packaging error (hook bundle missing).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from secondsight.cli._home import claude_home as resolve_claude_home
from secondsight.cli._home import codex_home as resolve_codex_home
from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.config.template import (
    MSG_MALFORMED,
    MSG_NEW_KEYS,
    write_config_if_needed,
)
from secondsight.installer import (
    ClaudeSettingsPatcher,
    CodexHooksPatcher,
    HookInstaller,
)
from secondsight.installer.claude_settings import InvalidSettingsError
from secondsight.installer.hook_install import HookBundleNotFoundError

app = typer.Typer(name="init", help="Install SecondSight hook scripts into Claude Code or Codex.")
_console = Console()

_SUPPORTED_AGENTS = frozenset({"claude_code", "codex"})


@app.callback(invoke_without_command=True)
def init(
    ctx: typer.Context,
    agent: str = typer.Option(
        "claude_code",
        "--agent",
        help="Target agent: 'claude_code' (default) or 'codex'.",
    ),
    agent_home: str = typer.Option(
        "",
        "--agent-home",
        help="Override the selected agent's home directory.",
    ),
    claude_home: str = typer.Option(
        "",
        "--claude-home",
        help="Override the Claude Code home directory (compatibility flag).",
    ),
    codex_home: str = typer.Option(
        "",
        "--codex-home",
        help="Override the Codex home directory.",
    ),
    hook_source: str = typer.Option(
        "",
        "--hook-source",
        help="Override the bundled hook directory (used by tests; auto-discovered otherwise).",
    ),
    secondsight_home_override: str = typer.Option(
        "",
        "--secondsight-home",
        help="Override the SecondSight home directory (~/.secondsight by default).",
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
    """Install SecondSight hooks into the chosen agent (idempotent)."""
    # Typer's callback fires on bare `secondsight init`. We do not want sub-
    # commands here; treat any subcommand invocation as an error so future
    # additions don't silently shadow the install behaviour.
    if ctx.invoked_subcommand is not None:  # pragma: no cover — no subcmds defined
        return

    canonical_agent = _normalize_agent(agent)
    target_agent_home = _resolve_agent_home(
        canonical_agent,
        agent_home=agent_home,
        claude_home=claude_home,
        codex_home=codex_home,
    )
    hook_dir = target_agent_home / "hooks"
    registration_path, patcher = _registration_target(canonical_agent, target_agent_home)
    ss_home = resolve_secondsight_home(secondsight_home_override)

    installer = HookInstaller(
        source_dir=Path(hook_source).expanduser() if hook_source else None,
    )

    # ----- Stage 0: validate registration file BEFORE touching hooks -----
    # The two stages (hook copy + settings patch) are not jointly transactional.
    # If we wrote hooks first and then discovered the registration file is malformed,
    # hooks would land on disk but never be registered — exactly the silent
    # failure mode this command must prevent. Calling plan() up-front raises
    # InvalidSettingsError on malformed JSON or wrong-typed sections WITHOUT
    # writing anything, so a failure here aborts before stage 1 ever runs.
    # In the non-dry-run path we still call apply() below; plan() is cheap
    # (single read + classify) and re-running it is harmless.
    try:
        precheck_plan = patcher.plan(hook_dir)
    except InvalidSettingsError as exc:
        _emit_error(output_format, "settings_invalid", str(exc))
        raise typer.Exit(code=1) from exc

    # ----- Stage 1: copy hook scripts -----
    try:
        install_plan = installer.install(hook_dir, dry_run=dry_run)
    except HookBundleNotFoundError as exc:
        _emit_error(output_format, "hook_bundle_missing", str(exc))
        raise typer.Exit(code=2) from exc

    # ----- Stage 2: apply (or report) registration patch -----
    if dry_run:
        patch_plan = precheck_plan
    else:
        try:
            patch_plan = patcher.apply(hook_dir)
        except InvalidSettingsError as exc:
            # The registration file was parseable at pre-check time but became invalid
            # between pre-check and apply (race with another writer). Hooks
            # are now on disk; surface the half-state honestly so the operator
            # can re-run after fixing the registration file.
            _emit_error(
                output_format,
                "settings_invalid_after_hook_copy",
                f"hooks copied but registration patch failed: {exc}. "
                f"Re-run `secondsight init` after fixing {registration_path}.",
            )
            raise typer.Exit(code=1) from exc

    # ----- Stage 3: generate (or diff-check) ~/.secondsight/config.toml -----
    # Never aborts on failure: config.toml issues are advisory (hook install
    # already succeeded). write_config_if_needed() returns a message string
    # rather than raising, so we can always print it and continue.
    config_status = write_config_if_needed(ss_home, dry_run=dry_run)

    summary = {
        "agent": canonical_agent,
        "agent_home": str(target_agent_home),
        "dry_run": dry_run,
        "hook_dir": str(hook_dir),
        "registration_path": str(registration_path),
        "settings_path": str(registration_path),
        "secondsight_home": str(ss_home),
        "config_status": config_status,
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
            "already registered in the agent config — review before running "
            "to avoid double-firing hooks:"
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
    _console.print(f"  agent:         {summary['agent']}")
    _console.print(f"  agent home:    {summary['agent_home']}")
    _console.print(f"  hook dir:      {summary['hook_dir']}")
    _console.print(f"  registration:  {summary['registration_path']}")
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

    # Stage 3: config.toml status.
    # MSG_MALFORMED and MSG_NEW_KEYS are imported from template.py so that
    # pattern-matching here stays in sync with the strings write_config_if_needed()
    # actually returns. A rename of either constant will cause an ImportError
    # immediately rather than silently losing coloring at runtime.
    config_status = summary.get("config_status", "")
    if isinstance(config_status, str) and config_status:
        # Use yellow for malformed or diff messages, normal for the rest
        if MSG_MALFORMED in config_status.lower():
            _console.print(f"  [yellow]config:[/yellow] {config_status.splitlines()[0]}")
        elif MSG_NEW_KEYS in config_status.lower():
            for line in config_status.splitlines():
                _console.print(
                    f"  [yellow]config:[/yellow] {line}"
                    if line.startswith(
                        summary.get("secondsight_home", "__no_match__")  # type: ignore[arg-type]
                    )
                    else f"  {line}"
                )
        else:
            _console.print(f"  config:        {config_status}")


def _normalize_agent(agent: str) -> str:
    candidate = agent.strip().lower()
    if candidate == "claude":
        candidate = "claude_code"
    if candidate not in _SUPPORTED_AGENTS:
        supported = ", ".join(sorted(_SUPPORTED_AGENTS))
        raise typer.BadParameter(
            f"Unsupported agent {agent!r}. Supported values: {supported}.",
            param_hint="--agent",
        )
    return candidate


def _resolve_agent_home(
    agent: str,
    *,
    agent_home: str,
    claude_home: str,
    codex_home: str,
) -> Path:
    if agent == "claude_code" and codex_home:
        raise typer.BadParameter(
            "--codex-home can only be used with --agent codex.",
            param_hint="--codex-home",
        )
    if agent == "codex" and claude_home:
        raise typer.BadParameter(
            "--claude-home can only be used with --agent claude_code.",
            param_hint="--claude-home",
        )

    if agent == "claude_code":
        chosen = agent_home or claude_home
        return resolve_claude_home(chosen)

    chosen = agent_home or codex_home
    return resolve_codex_home(chosen)


def _registration_target(agent: str, agent_home: Path) -> tuple[Path, object]:
    if agent == "claude_code":
        settings_path = agent_home / "settings.json"
        return settings_path, ClaudeSettingsPatcher(settings_path=settings_path)

    hooks_path = agent_home / "hooks.json"
    return hooks_path, CodexHooksPatcher(hooks_path=hooks_path)


__all__ = ["app"]
