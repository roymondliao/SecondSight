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

  DT-8  `secondsight status` per-project counting errors must NOT
        crash the entire status command. One corrupt project must
        surface an error dict and let healthy projects report
        normally. GUR-130 finding #3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from secondsight.api.registry import ProjectRegistry
from secondsight.cli.app import app
from secondsight.installer.claude_settings import InvalidSettingsError

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


# ---------------------------------------------------------------------------
# DT-6 (review N2 / MUST-FIX-NOW): unknown subcommand must produce a clean
# Click usage error (exit 2, no traceback) — not a Rich traceback with exit 1.
#
# Background: standalone_mode=False (DT-5 fix) made Click *return* exit codes
# for typer.Exit but left click.ClickException uncaught — so `secondsight bogus`
# propagates UsageError up to Python's default handler. Result: a Rich
# traceback in the user's terminal and exit 1, which violates the GUR-112
# install-smoke exit-code contract (usage errors == exit 2 by Click
# convention). Phase 2 wrappers parsing exit codes would silently misread
# "user typo" as "task failed".
# ---------------------------------------------------------------------------


def test_death_unknown_subcommand_exits_two_no_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from secondsight.cli.app import main as cli_main

    code = cli_main(["bogus"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert code == 2, (
        f"unknown subcommand must exit 2 (Click usage-error convention), got {code!r}; "
        f"a regression here breaks GUR-112 install-smoke and Phase 2 wrappers. "
        f"Output was:\n{combined}"
    )
    assert "No such command" in combined, (
        f"Click's standard usage message must surface to the user, got:\n{combined}"
    )
    assert "Traceback" not in combined, (
        f"unknown subcommand must NOT produce a Rich/Python traceback; "
        f"a leak here means click.ClickException is uncaught again. Got:\n{combined}"
    )


def test_death_unknown_flag_exits_two_no_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sibling check: ``--no-such-flag`` is a UsageError too.

    Catching ClickException at the parent level — not just UsageError —
    means BadParameter/MissingParameter also exit cleanly.
    """
    from secondsight.cli.app import main as cli_main

    code = cli_main(["init", "--no-such-flag"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert code == 2, (
        f"unknown flag must exit 2 (Click usage-error convention), got {code!r}; "
        f"output:\n{combined}"
    )
    assert "Traceback" not in combined, (
        f"unknown flag must not produce a traceback; got:\n{combined}"
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


def test_init_codex_apply_then_idempotent(tmp_path: Path) -> None:
    fake_codex = tmp_path / "codex"
    first = runner.invoke(
        app,
        ["init", "--agent", "codex", "--codex-home", str(fake_codex), "--format", "json"],
    )
    assert first.exit_code == 0, first.output
    payload_first = json.loads(first.output)
    assert payload_first["agent"] == "codex"
    assert payload_first["registration_path"].endswith("hooks.json")
    assert payload_first["scripts_copied"], "first Codex run should copy scripts"

    hooks_json = fake_codex / "hooks.json"
    assert hooks_json.is_file(), f"hooks.json not created at {hooks_json}"

    second = runner.invoke(
        app,
        ["init", "--agent", "codex", "--codex-home", str(fake_codex), "--format", "json"],
    )
    assert second.exit_code == 0, second.output
    payload_second = json.loads(second.output)
    assert payload_second["scripts_copied"] == [], "second Codex run should be a no-op for scripts"
    assert all(action == "skip" for action in payload_second["settings_actions"].values()), (
        f"second Codex run should skip every action, got {payload_second['settings_actions']!r}"
    )


def test_death_init_rejects_mismatched_agent_home_flag(tmp_path: Path) -> None:
    fake_claude = tmp_path / "claude"
    result = runner.invoke(
        app,
        [
            "init",
            "--agent",
            "codex",
            "--claude-home",
            str(fake_claude),
        ],
    )
    assert result.exit_code == 2, (
        f"mismatched per-agent home flag must be a CLI usage error, got {result.exit_code}; "
        f"output={result.output!r}"
    )
    assert "Invalid value for --claude-home" in result.output
    assert "--agent" in result.output
    assert "claude_code." in result.output


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
# DT-8: status per-project hardening (GUR-130 finding #3)
# ---------------------------------------------------------------------------


def test_status_positive_path_reports_project_counts(tmp_path: Path) -> None:
    """Status command with a valid project must report event/session/pending
    counts (not just the empty-home case). GUR-130 finding #3.
    """
    home = tmp_path / "ss"
    project_dir = home / "projects" / "proj1"
    project_dir.mkdir(parents=True)
    (project_dir / "sessions" / "s1").mkdir(parents=True)
    (project_dir / "sessions" / "s2").mkdir(parents=True)

    result = runner.invoke(app, ["status", "--home", str(home), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    projects = payload["projects"]
    assert len(projects) == 1
    p = projects[0]
    assert p["project_id"] == "proj1"
    assert p["error"] is None
    assert p["events_in_db"] == 0
    assert p["sessions_on_disk"] == 2
    assert p["sync_log_pending"] == 0


def test_death_status_corrupt_project_does_not_crash_healthy_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One project whose counting ops raise must NOT prevent healthy projects
    from reporting. GUR-130 finding #3 — asymmetry with sync.py's S1 hardening.
    """
    home = tmp_path / "ss"
    (home / "projects" / "aaa_corrupt").mkdir(parents=True)
    (home / "projects" / "zzz_healthy").mkdir(parents=True)
    (home / "projects" / "zzz_healthy" / "sessions" / "s1").mkdir(parents=True)

    real_build = ProjectRegistry._build_resources  # noqa: SLF001

    call_count = 0

    def build_with_corrupt_first(self, project_id):
        nonlocal call_count
        call_count += 1
        resources = real_build(self, project_id)
        if project_id == "aaa_corrupt":
            resources.db_engine.dispose()
            raise RuntimeError("simulated corrupt DB")
        return resources

    monkeypatch.setattr(ProjectRegistry, "_build_resources", build_with_corrupt_first)

    result = runner.invoke(app, ["status", "--home", str(home), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    projects = {p["project_id"]: p for p in payload["projects"]}

    assert "aaa_corrupt" in projects
    assert projects["aaa_corrupt"]["error"] is not None
    assert "RuntimeError" in projects["aaa_corrupt"]["error"]

    assert "zzz_healthy" in projects
    assert projects["zzz_healthy"]["error"] is None, (
        "healthy project must still report normally after corrupt project"
    )
    assert projects["zzz_healthy"]["sessions_on_disk"] == 1


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
# DT-9 (GUR-130 #5): settings_invalid_after_hook_copy race-window pins the
# half-state branch — hooks dir present, error code distinguishes from pre-check.
# ---------------------------------------------------------------------------


def test_death_init_settings_invalid_after_hook_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkey-patch ClaudeSettingsPatcher.apply to raise InvalidSettingsError
    AFTER hook scripts have been copied. The half-state branch must:
    1. Report error == "settings_invalid_after_hook_copy" (NOT "settings_invalid")
    2. Leave the hooks dir on disk (hooks were already written)
    GUR-130 finding #5.
    """
    fake_claude = tmp_path / "claude"
    fake_claude.mkdir()
    (fake_claude / "settings.json").write_text("{}", encoding="utf-8")

    from secondsight.installer.claude_settings import ClaudeSettingsPatcher

    real_plan = ClaudeSettingsPatcher.plan

    def apply_raising(self, hook_dir):
        raise InvalidSettingsError("simulated race: settings changed between plan and apply")

    monkeypatch.setattr(ClaudeSettingsPatcher, "apply", apply_raising)

    result = runner.invoke(
        app,
        ["init", "--claude-home", str(fake_claude), "--format", "json"],
    )
    assert result.exit_code == 1, f"got {result.exit_code}, output={result.output}"
    payload = json.loads(result.output)
    assert payload["error"] == "settings_invalid_after_hook_copy", (
        f"half-state must use 'settings_invalid_after_hook_copy', "
        f"not 'settings_invalid'; got {payload!r}"
    )
    assert (fake_claude / "hooks").exists(), (
        "hooks dir MUST exist in the half-state — hooks were already copied "
        "before the settings patch failed"
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


# ---------------------------------------------------------------------------
# DT-7 (review S1 / SHOULD-FIX-NOW): multi-project mixed-failure path is
# pinned. The S1 fix in cli/sync.py wraps each project's _build_resources()
# and backfill.run() in try/except so one project's failure does NOT abort
# the loop. Without this test, a Phase 2 refactor that drops the wrap
# silently passes test_death_sync_exits_nonzero_on_failure (single-project)
# AND test_death_zero_project_sync_still_archives_fallback (zero-project),
# but production would lose every project listed after the first crash.
# ---------------------------------------------------------------------------


def test_death_sync_multi_project_failure_does_not_abort_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two projects, first one's _build_resources raises mid-loop.

    The honest signal: BOTH projects must appear in the JSON `projects`
    list — the failed one with `error != null` and `backfill == null`,
    the healthy one with `error == null` and a populated backfill report.
    If the per-project try/except in cli/sync.py is reverted, the second
    project never appears (the exception bubbles out of the for-loop) and
    this test goes RED at the `project_ids == {alpha, bravo}` assertion.
    """
    home = tmp_path / "ss"
    (home / "projects" / "alpha").mkdir(parents=True)
    (home / "projects" / "bravo").mkdir(parents=True)

    real_build = ProjectRegistry._build_resources

    def fake_build(self: ProjectRegistry, project_id: str):  # type: ignore[no-untyped-def]
        if project_id == "alpha":
            raise RuntimeError("simulated DB death for alpha")
        return real_build(self, project_id)

    monkeypatch.setattr(ProjectRegistry, "_build_resources", fake_build)

    result = runner.invoke(
        app,
        ["sync", "--home", str(home), "--format", "json", "--no-fallback-archive"],
    )

    assert result.exit_code == 1, (
        f"any-project failure must surface as exit 1, got {result.exit_code}; "
        f"output={result.output}"
    )
    payload = json.loads(result.output)
    project_ids = {p["project_id"] for p in payload["projects"]}
    assert project_ids == {"alpha", "bravo"}, (
        f"both projects MUST appear in reports — a regression that drops the "
        f"per-project wrap would lose 'bravo' when 'alpha' crashes. "
        f"Got project_ids={project_ids!r}, full payload={payload!r}"
    )

    by_id = {p["project_id"]: p for p in payload["projects"]}
    alpha = by_id["alpha"]
    bravo = by_id["bravo"]

    assert alpha["backfill"] is None, f"failed project must report backfill=null, got {alpha!r}"
    assert alpha["error"] is not None and "RuntimeError" in alpha["error"], (
        f"failed project's error must surface RuntimeError + message; got {alpha['error']!r}"
    )

    assert bravo["error"] is None, (
        f"healthy project must report error=null even when a sibling failed; got {bravo!r}"
    )
    assert bravo["backfill"] is not None, (
        "healthy project must produce a real backfill report (proves the "
        "loop continued past the alpha failure), got backfill=null"
    )


# ---------------------------------------------------------------------------
# DT-10 (GUR-130 #6): --project-id path-traversal rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc",
        "../passwd",
        "foo/bar",
        "foo\\bar",
        "..",
        ".",
        "valid\x00inject",
    ],
    ids=[
        "dotdot-slash-traversal",
        "parent-traversal",
        "slash-in-id",
        "backslash-in-id",
        "bare-dotdot",
        "bare-dot",
        "null-byte",
    ],
)
def test_death_sync_rejects_unsafe_project_id(tmp_path: Path, bad_id: str) -> None:
    """--project-id with path-traversal characters must be rejected before
    any filesystem operation. GUR-130 finding #6 — HARD GATE for Phase 2.
    """
    home = tmp_path / "ss"
    home.mkdir()

    result = runner.invoke(
        app,
        ["sync", "--home", str(home), "--format", "json", "--project-id", bad_id],
    )
    assert result.exit_code != 0, (
        f"unsafe project-id {bad_id!r} must be rejected, "
        f"got exit_code={result.exit_code}, output={result.output}"
    )


def test_sync_accepts_safe_project_id(tmp_path: Path) -> None:
    """A safe project-id that exists on disk must be accepted. GUR-130 #6."""
    home = tmp_path / "ss"
    (home / "projects" / "my-project").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "sync",
            "--home",
            str(home),
            "--format",
            "json",
            "--project-id",
            "my-project",
            "--no-fallback-archive",
        ],
    )
    assert result.exit_code == 0, (
        f"safe project-id 'my-project' must be accepted, "
        f"got exit_code={result.exit_code}, output={result.output}"
    )
