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
    secondsight init --merge-config         # fill missing config keys safely

Exit codes:
    0 on success (or clean dry-run);
    1 on user-actionable error (invalid registration file, double install warned);
    2 on packaging error (hook bundle missing).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import typer
from loguru import logger
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
from secondsight.installer.claude_settings import InvalidSettingsError, PatchPlan
from secondsight.installer.hook_install import HookBundleNotFoundError
from secondsight.cli._typer import create_typer
from secondsight.state import SecondSightState, SecondSightStateError, make_state

app = create_typer(name="init", help="Install SecondSight hook scripts into Claude Code or Codex.")
_console = Console()

_SUPPORTED_AGENTS = frozenset({"claude_code", "codex"})
_HOOK_RUNTIME_FILENAME = ".secondsight-hook-runtime.sh"


class RegistrationPatcher(Protocol):
    def plan(self, hook_dir: Path) -> PatchPlan: ...

    def apply(self, hook_dir: Path) -> PatchPlan: ...


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
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing state.json without prompting (for scripted re-init).",
    ),
    merge_config: bool = typer.Option(
        False,
        "--merge-config",
        help="Fill missing keys in an existing config.toml without overriding existing values.",
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

    # ----- Stage 2.5: pin hook Python runtime -----
    hook_runtime_status = _write_hook_runtime_file(
        hook_dir=hook_dir,
        dry_run=dry_run,
    )

    # ----- Stage 2.6: write state.json (init_agent persistence) -----
    # DC11: prompt on overwrite — silent overwrite is the lie.
    # Exception: --force bypasses prompt for scripted re-init flows.
    # Dry-run: skip writing but include state_status in summary.
    state_status = _write_state_json(
        ss_home=ss_home,
        canonical_agent=canonical_agent,
        dry_run=dry_run,
        force=force,
    )

    # ----- Stage 3: generate (or diff-check) ~/.secondsight/config.toml -----
    # Never aborts on failure: config.toml issues are advisory (hook install
    # already succeeded). write_config_if_needed() returns a message string
    # rather than raising, so we can always print it and continue.
    config_status = write_config_if_needed(
        ss_home,
        dry_run=dry_run,
        merge_missing_keys=merge_config,
    )

    summary = {
        "agent": canonical_agent,
        "agent_home": str(target_agent_home),
        "dry_run": dry_run,
        "hook_dir": str(hook_dir),
        "registration_path": str(registration_path),
        "settings_path": str(registration_path),
        "secondsight_home": str(ss_home),
        "hook_runtime_path": str(hook_dir / _HOOK_RUNTIME_FILENAME),
        "hook_runtime_status": hook_runtime_status,
        "hook_runtime_python": sys.executable,
        "config_status": config_status,
        "state_status": state_status,
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


def _write_state_json(
    *,
    ss_home: Path,
    canonical_agent: str,
    dry_run: bool,
    force: bool,
) -> str:
    """Write (or check) ~/.secondsight/state.json after hook install.

    DC11: if state.json already exists with a different init_agent, prompt the user
    before overwriting. Default answer is N (preserve existing). --force skips the prompt.

    Args:
        ss_home: SecondSight home directory (~/.secondsight).
        canonical_agent: The agent being installed ("claude_code" or "codex").
        dry_run: If True, report what would be done without writing.
        force: If True, overwrite without prompting.

    Returns:
        A human-readable status string for inclusion in the summary output.
    """
    state_path = ss_home / "state.json"

    if dry_run:
        return f"dry-run: would write state.json (init_agent={canonical_agent!r})"

    # Check for existing state with a different agent
    existing_state: SecondSightState | None = None
    try:
        existing_state = SecondSightState.load(state_path)
    except SecondSightStateError as exc:
        # Malformed existing state — warn but proceed (overwrite with valid state)
        logger.warning(f"existing state.json is malformed, overwriting: {exc}")
        existing_state = None

    if existing_state is not None and existing_state.init_agent != canonical_agent:
        if not force:
            # DC11: prompt before overwriting
            answer = typer.confirm(
                f"state.json already exists with init_agent={existing_state.init_agent!r}. "
                f"Overwrite with {canonical_agent!r}?",
                default=False,
            )
            if not answer:
                return f"state.json unchanged (kept init_agent={existing_state.init_agent!r})"

    # Write new state
    new_state = make_state(canonical_agent)
    try:
        new_state.save(state_path)
        return f"state.json written (init_agent={canonical_agent!r})"
    except OSError as exc:
        # Non-fatal: state write failure is advisory (hook install already succeeded)
        logger.warning(f"state.json write failed (non-fatal): {exc}")
        return f"state.json write failed: {exc}"


def _write_hook_runtime_file(*, hook_dir: Path, dry_run: bool) -> str:
    """Write the pinned Python launcher used by hook-time Python helpers.

    Death clause: if init leaves no pinned runtime file, hook execution falls
    back to environment guessing and can silently fail-open on machines where
    PATH differs from the shell used during installation.
    """
    runtime_path = hook_dir / _HOOK_RUNTIME_FILENAME
    body = (
        "#!/usr/bin/env bash\n"
        "# Generated by `secondsight init`.\n"
        f"SECONDSIGHT_HOOK_PYTHON={shlex.quote(sys.executable)}\n"
    )
    if dry_run:
        return f"dry-run: would write hook runtime ({sys.executable})"

    try:
        hook_dir.mkdir(parents=True, exist_ok=True)
        if runtime_path.is_file():
            try:
                if runtime_path.read_text(encoding="utf-8") == body:
                    return f"hook runtime unchanged ({sys.executable})"
            except OSError:
                pass

        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_hook_runtime_",
            dir=str(hook_dir),
        )
        os.close(fd)
        try:
            Path(tmp_path).write_text(body, encoding="utf-8")
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, runtime_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return f"hook runtime written ({sys.executable})"
    except OSError as exc:
        logger.warning(f"hook runtime write failed (non-fatal): {exc}")
        return f"hook runtime write failed: {exc}"


def _emit_error(output_format: str, code: str, message: str) -> None:
    if output_format == "json":
        typer.echo(json.dumps({"error": code, "message": message}, indent=2))
    else:
        _console.print(f"[red]error[/red] ({code}): {message}")


def _render_text(summary: Mapping[str, object]) -> None:
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
        _console.print(f"  hooks {verb}: {', '.join(cast(list[str], copied))}")
    if skipped:
        _console.print(f"  hooks unchanged: {', '.join(cast(list[str], skipped))}")

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

    hook_runtime_status = summary.get("hook_runtime_status", "")
    if isinstance(hook_runtime_status, str) and hook_runtime_status:
        if "write failed" in hook_runtime_status:
            _console.print(f"  [yellow]hook runtime:[/yellow] {hook_runtime_status}")
        elif "dry-run" in hook_runtime_status:
            _console.print(f"  [dim]hook runtime: {hook_runtime_status}[/dim]")
        else:
            _console.print(f"  hook runtime: {hook_runtime_status}")

    # Stage 2.5: state.json write status.
    # Surfaces all state_status outcomes so silent failures (disk full, permission denied)
    # are visible to the user. Without this block the user sees "[cyan]Installed[/cyan]"
    # even when state.json write failed — Task 6's "auto" resolution would then find no
    # state.json and fall through to surprising defaults with no user-visible signal.
    state_status = summary.get("state_status", "")
    if isinstance(state_status, str) and state_status:
        if "write failed" in state_status:
            # Critical: state.json was not written — "auto" resolution will not work.
            _console.print(f"  [bold red]state:[/bold red]  {state_status}")
        elif "unchanged" in state_status or "declined" in state_status:
            # Advisory: user declined overwrite — existing state preserved.
            _console.print(f"  [yellow]state:[/yellow]  {state_status}")
        elif "dry-run" in state_status or "dry_run" in state_status:
            # Informational: dry-run mode, nothing written.
            _console.print(f"  [dim]state:  {state_status}[/dim]")
        else:
            # Normal: written or unchanged (idempotent re-init).
            _console.print(f"  state:  {state_status}")

    # Stage 3: config.toml status.
    # MSG_MALFORMED and MSG_NEW_KEYS are imported from template.py so that
    # pattern-matching here stays in sync with the strings write_config_if_needed()
    # actually returns. A rename of either constant will cause an ImportError
    # immediately rather than silently losing coloring at runtime.
    config_status = summary.get("config_status", "")
    if isinstance(config_status, str) and config_status:
        secondsight_home_prefix = summary.get("secondsight_home", "__no_match__")
        if not isinstance(secondsight_home_prefix, str):
            secondsight_home_prefix = "__no_match__"
        # Use yellow for malformed or diff messages, normal for the rest
        if MSG_MALFORMED in config_status.lower():
            _console.print(f"  [yellow]config:[/yellow] {config_status.splitlines()[0]}")
        elif MSG_NEW_KEYS in config_status.lower():
            for line in config_status.splitlines():
                _console.print(
                    f"  [yellow]config:[/yellow] {line}"
                    if line.startswith(secondsight_home_prefix)
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


def _registration_target(agent: str, agent_home: Path) -> tuple[Path, RegistrationPatcher]:
    if agent == "claude_code":
        settings_path = agent_home / "settings.json"
        return settings_path, ClaudeSettingsPatcher(settings_path=settings_path)

    hooks_path = agent_home / "hooks.json"
    return hooks_path, CodexHooksPatcher(hooks_path=hooks_path)


__all__ = ["app"]
