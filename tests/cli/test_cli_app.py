"""Top-level Typer CLI tests (GUR-98 / P1-12).

Verifies the four subcommands are reachable via the entry point and that
``--format json`` produces parseable output for agent consumers (SD §9.1).
We use Typer's CliRunner rather than subprocess so we can intercept the
result objects without spinning up the full uvicorn process.

Death tests:
  DT-1  `secondsight init --dry-run --format json` produces valid JSON and
        does NOT touch ~/.claude/. A regression where dry-run accidentally
        wrote files would silently overwrite user setup on every preview.
  DT-2  `secondsight sync` exits 1 when any project's backfill reports
        a failure (so operators / scripts notice). A regression that
        always returned 0 would mask data-loss conditions.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from secondsight.cli.app import app

runner = CliRunner()


def test_top_level_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("init", "serve", "status", "sync", "version"):
        assert name in result.output, f"expected {name!r} in top-level help, got:\n{result.output}"


# ---------------------------------------------------------------------------
# DT-5 (review): main() must propagate non-zero exit codes from subcommands
# back through `python -m secondsight`. Discovered via smoke test of the
# init pre-check fix: with standalone_mode=False, Click returns the exit
# code rather than re-raising, so the wrapper must capture the return.
# Without this, `secondsight init` against a malformed settings.json would
# print the JSON error AND silently exit 0, hiding the failure from CI.
# ---------------------------------------------------------------------------


def test_death_main_propagates_nonzero_exit_code(tmp_path: Path) -> None:
    from secondsight.cli.app import main as cli_main

    fake_claude = tmp_path / "claude"
    fake_claude.mkdir()
    (fake_claude / "settings.json").write_text("{ broken", encoding="utf-8")

    code = cli_main(["init", "--claude-home", str(fake_claude), "--format", "json"])
    assert code == 1, (
        f"main() must return 1 on malformed-settings exit, got {code!r}; "
        f"a regression here would hide failures from `python -m secondsight`."
    )


def test_version_subcommand() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "secondsight " in result.output


# ---------------------------------------------------------------------------
# DT-1: init --dry-run --format json is non-destructive
# ---------------------------------------------------------------------------


def test_death_init_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    fake_claude = tmp_path / "claude"
    result = runner.invoke(
        app,
        [
            "init",
            "--dry-run",
            "--format",
            "json",
            "--claude-home",
            str(fake_claude),
        ],
    )
    assert result.exit_code == 0, f"got {result.exit_code}, output={result.output}"
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    # Settings file is the strongest signal — even an empty dir would have
    # let a regression succeed at the filesystem level.
    assert not (fake_claude / "settings.json").exists(), "dry-run must NOT create settings.json"
    assert not (fake_claude / "hooks").exists(), "dry-run must NOT create the hooks/ dir"


def test_init_apply_then_idempotent(tmp_path: Path) -> None:
    fake_claude = tmp_path / "claude"
    first = runner.invoke(
        app,
        ["init", "--claude-home", str(fake_claude), "--format", "json"],
    )
    assert first.exit_code == 0, first.output
    payload_first = json.loads(first.output)
    assert payload_first["scripts_copied"], "first run should copy scripts"

    second = runner.invoke(
        app,
        ["init", "--claude-home", str(fake_claude), "--format", "json"],
    )
    assert second.exit_code == 0, second.output
    payload_second = json.loads(second.output)
    assert payload_second["scripts_copied"] == [], "second run should be a no-op for scripts"
    assert all(action == "skip" for action in payload_second["settings_actions"].values()), (
        f"second run should skip every settings action, got {payload_second['settings_actions']!r}"
    )


# ---------------------------------------------------------------------------
# Status — empty home is benign (server not running, no projects)
# ---------------------------------------------------------------------------


def test_status_on_empty_home_returns_clean_json(tmp_path: Path) -> None:
    fake_home = tmp_path / "ss"
    result = runner.invoke(app, ["status", "--home", str(fake_home), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["server"]["running"] is False
    assert payload["projects"] == []


# ---------------------------------------------------------------------------
# DT-2: sync exits 1 when failures present
# ---------------------------------------------------------------------------


def test_death_sync_exits_nonzero_on_failure(tmp_path: Path) -> None:
    """Drive sync against a project whose sync_log points at a missing file.

    ProjectRegistry materialises the project lazily; we set up the
    filesystem to look like a half-broken install, then run `sync`.
    """
    home = tmp_path / "ss"
    project_dir = home / "projects" / "pid"
    project_dir.mkdir(parents=True)
    # Pre-write a sync_log entry referencing a path that does not exist.
    sync_log = project_dir / "sync.log"
    sync_log.write_text(
        json.dumps(
            {
                "event_id": "evt-broken",
                "raw_trace_path": str(tmp_path / "missing.json"),
                "error_class": "RuntimeError",
                "error_message": "simulated",
                "timestamp": "2026-05-05T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["sync", "--home", str(home), "--format", "json", "--no-fallback-archive"],
    )
    assert result.exit_code == 1, (
        f"sync must exit 1 when failures are present, got {result.exit_code}; "
        f"output={result.output}"
    )
    payload = json.loads(result.output)
    assert payload["projects"][0]["backfill"]["sync_log_remaining"] == 1
    assert payload["projects"][0]["backfill"]["failures"], (
        "failures list must surface to JSON consumers"
    )


# ---------------------------------------------------------------------------
# DT-3 (review C1): init pre-check rejects malformed settings.json BEFORE
# copying any hook scripts to disk.
# ---------------------------------------------------------------------------


def test_death_init_aborts_before_hook_copy_when_settings_malformed(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.mkdir()
    bad = fake_claude / "settings.json"
    bad.write_text("{ this is not json", encoding="utf-8")

    result = runner.invoke(
        app,
        ["init", "--claude-home", str(fake_claude), "--format", "json"],
    )
    assert result.exit_code == 1, f"got {result.exit_code}, output={result.output}"
    payload = json.loads(result.output)
    assert payload["error"] == "settings_invalid", f"expected pre-check rejection, got {payload!r}"
    # Critical: hooks/ directory must NOT exist. A regression that copied
    # hooks before validating settings.json would silently leave a
    # half-state (hooks on disk, never registered).
    assert not (fake_claude / "hooks").exists(), (
        "init must NOT copy hooks when settings.json is malformed"
    )


# ---------------------------------------------------------------------------
# DT-4 (review C3): zero-project sync still archives a populated fallback
# file. Without this, a fresh ~/.secondsight with accumulated fallback
# events would silently never rotate the file.
# ---------------------------------------------------------------------------


def test_death_zero_project_sync_still_archives_fallback(tmp_path: Path) -> None:
    home = tmp_path / "ss"
    home.mkdir()
    fb = home / "fallback_events.jsonl"
    fb.write_text(
        json.dumps({"agent": "claude_code", "event_type": "session_start"}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["sync", "--home", str(home), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["projects"] == []
    archive = payload["fallback_archive"]
    assert archive is not None and archive["archived"] is True, (
        f"zero-project sync must still archive populated fallback file, got {archive!r}"
    )
    # Live file moved away.
    assert not fb.exists()
