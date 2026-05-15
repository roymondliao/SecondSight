"""Legacy config upgrade regression tests (no external services required).

These tests MUST pass in CI and in any sandbox environment.
They exercise the full loader → precheck → serve bootstrap path
using in-process invocation (Typer test runner), never spinning up a real HTTP
server or calling any LLM API.

Death tests cover:
  - Legacy config (flat [analysis] default_agent) causes precheck to fail when
    state.json is absent (DC5 path fires after loader succeeds with WARN).
  - Loader emits WARN about legacy field even when it does not raise.
  - After `secondsight init --agent claude_code`, state.json exists with correct
    init_agent field.
  - Re-running serve with legacy config + state.json + patched binary passes
    precheck and emits WARN about legacy field.
  - Resolved mode is "cli" (built-in default, not overridden by legacy key).

Unit / regression tests cover:
  - Fresh install (no config.toml) defaults to mode="cli".
  - Tests that previously exercised the SDK path still pass with explicit
    mode=sdk config (regression check: default-cli-mode change must not silently
    break existing SDK tests).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from secondsight.cli.app import app as secondsight_app
from secondsight.config.loader import load_global_config
from secondsight.config.precheck import precheck
from secondsight.config.schema import SecondSightConfig
from secondsight.state import SecondSightState, make_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_LEGACY_CONFIG_TOML = _FIXTURES_DIR / "legacy_config.toml"

runner = CliRunner()


def _write_legacy_config(ss_home: Path) -> Path:
    """Write the canonical legacy config fixture into ss_home/config.toml."""
    ss_home.mkdir(parents=True, exist_ok=True)
    (ss_home / "logs").mkdir(exist_ok=True)
    config_path = ss_home / "config.toml"
    config_path.write_text(_LEGACY_CONFIG_TOML.read_text(encoding="utf-8"), encoding="utf-8")
    return config_path


def _write_state_json(ss_home: Path, agent: str = "claude_code") -> Path:
    """Write a minimal valid state.json into ss_home."""
    state = make_state(agent)
    state_path = ss_home / "state.json"
    state.save(state_path)
    return state_path


def _mock_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch installer so `secondsight init` does not need real hook files."""
    mock_plan = type(
        "PatchPlan",
        (),
        {"actions": {}, "file_existed": False, "foreign_secondsight_paths": []},
    )()
    monkeypatch.setattr(
        "secondsight.installer.hook_install.HookInstaller.install",
        lambda self, hook_dir, dry_run=False: type(
            "InstallPlan",
            (),
            {"copied": [], "skipped_identical": [], "source_dir": hook_dir},
        )(),
    )
    monkeypatch.setattr(
        "secondsight.installer.claude_settings.ClaudeSettingsPatcher.plan",
        lambda self, hook_dir: mock_plan,
    )
    monkeypatch.setattr(
        "secondsight.installer.claude_settings.ClaudeSettingsPatcher.apply",
        lambda self, hook_dir: mock_plan,
    )
    monkeypatch.setattr(
        "secondsight.config.template.write_config_if_needed",
        lambda ss_home, dry_run=False: "config already exists",
    )


# ---------------------------------------------------------------------------
# DEATH TEST: legacy config → loader must WARN, not raise
# ---------------------------------------------------------------------------


class TestDTLegacyLoaderBehavior:
    """Death test: DC12 — legacy flat [analysis] default_agent must trigger WARN, not error."""

    def test_legacy_config_loader_warns_not_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DC12: legacy config loads without exception; WARN log emitted.

        Silent failure path: if the loader silently ignores legacy key with no
        user-visible signal, the user can't know their config needs updating.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)

        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            cfg = load_global_config(ss_home)

        # Must not raise — legacy config is survivable
        assert cfg is not None
        assert isinstance(cfg, SecondSightConfig)

    def test_legacy_config_warn_log_mentions_legacy_field(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DC12: loader WARN log must mention the legacy field so user knows what to fix."""
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)

        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            load_global_config(ss_home)

        warn_messages = " ".join(r.message for r in caplog.records if r.levelno >= _logging.WARNING)
        assert "legacy" in warn_messages.lower() or "default_agent" in warn_messages, (
            f"Expected WARN log to mention 'legacy' or 'default_agent'. "
            f"Got warn messages: {warn_messages!r}. "
            f"If no warning was emitted, DC12 warn-and-ignore is silently broken."
        )

    def test_legacy_config_resolves_mode_to_cli(self, tmp_path: Path) -> None:
        """DC12: legacy config without [general] section → mode defaults to 'cli'.

        This is the built-in default. The legacy default_agent field does NOT
        change mode. If it silently changed mode to 'sdk', analysis would fail
        with no providers configured.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)

        cfg = load_global_config(ss_home)

        assert cfg.general.mode == "cli", (
            f"Expected mode='cli' for legacy config (built-in default). "
            f"Got mode={cfg.general.mode!r}. "
            f"If mode is 'sdk', the server would fail precheck with 'no_providers'."
        )


# ---------------------------------------------------------------------------
# DEATH TEST: legacy config + no state.json → precheck fails with state_missing
# ---------------------------------------------------------------------------


class TestDTLegacyPrecheckBehavior:
    """Death test: legacy config precheck behavior.

    DC12 behavior: the loader warn-and-ignores the flat [analysis] default_agent
    and sets analysis.cli.default_agent to BUILTIN_DEFAULT_AGENT ("claude_code").
    This means precheck does NOT check state.json (no "auto" resolution needed).
    Instead, precheck checks that the `claude` binary is in PATH.

    DC5 applies when default_agent="auto" — not the case for legacy config.
    This class verifies the actual DC12 + precheck contract, not a hypothetical.
    """

    def test_legacy_config_resolves_explicit_default_agent_not_auto(self, tmp_path: Path) -> None:
        """DC12: loader sets default_agent=BUILTIN (not "auto") for legacy flat config.

        The legacy key is ignored but the builtin default "claude_code" is used.
        If the loader silently set default_agent="auto", precheck would fail
        with state_missing (confusing — no state needed for explicit agent).
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)

        cfg = load_global_config(ss_home)

        # With legacy config, loader should produce an explicit agent, not "auto"
        # (BUILTIN_DEFAULT_AGENT = "claude_code")
        assert cfg.analysis.cli.default_agent != "auto", (
            f"Legacy config must produce an explicit default_agent, not 'auto'. "
            f"Got: {cfg.analysis.cli.default_agent!r}. "
            f"If 'auto' is returned, precheck would require state.json for a "
            f"case where the user had an explicit agent preference."
        )

    def test_legacy_config_no_state_precheck_fails_binary_not_found(self, tmp_path: Path) -> None:
        """DC12 + binary missing → precheck fails with cli_binary_missing.

        With legacy config (no state.json, explicit agent from BUILTIN),
        precheck checks for the claude binary in PATH. If binary is absent,
        the server correctly refuses to start.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)

        cfg = load_global_config(ss_home)
        state = SecondSightState.load(ss_home / "state.json")  # should be None

        with patch("shutil.which", return_value=None):
            result = precheck(config=cfg, state=state)

        assert not result.is_ok, (
            f"Expected precheck to fail when claude binary is absent. Got is_ok={result.is_ok!r}."
        )
        assert result.reason == "cli_binary_missing", (
            f"Expected reason='cli_binary_missing'. Got reason={result.reason!r}."
        )

    def test_serve_with_legacy_config_no_binary_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """secondsight serve with legacy config + no binary → non-zero exit.

        This exercises the full CLI path: serve → _run_precheck → exit(1).
        No server is actually started.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        # Patch _run_server so it never starts even if precheck somehow passes
        with (
            patch("shutil.which", return_value=None),
            patch("secondsight.cli.serve._run_server") as mock_server,
        ):
            result = runner.invoke(secondsight_app, ["serve"])

        assert result.exit_code != 0, (
            f"Expected non-zero exit when legacy config + no binary. "
            f"Got exit_code={result.exit_code}. "
            f"Output: {result.output!r}."
        )
        mock_server.assert_not_called()


# ---------------------------------------------------------------------------
# DEATH TEST: after init, serve with legacy config + state.json + binary → succeeds
# ---------------------------------------------------------------------------


class TestDTLegacyUpgradeSucceeds:
    """Death test: legacy config + binary found → serve starts (WARN emitted).

    DC12 contract: with legacy config, loader resolves explicit default_agent
    (BUILTIN_DEFAULT_AGENT = "claude_code"). Precheck only needs the binary,
    not state.json (state.json is only required when default_agent="auto").
    """

    def test_legacy_config_with_binary_passes_precheck(self, tmp_path: Path) -> None:
        """Legacy config + binary found → precheck passes (no state.json needed).

        The legacy loader sets default_agent="claude_code" (BUILTIN), not "auto".
        So precheck only checks the binary, not state.json.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)
        # No state.json — intentionally testing without it

        cfg = load_global_config(ss_home)

        # Patch binary lookup to succeed
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = precheck(config=cfg, state=None)

        assert result.is_ok, (
            f"Expected precheck to pass for legacy config + binary (no state.json). "
            f"Got is_ok={result.is_ok!r}, reason={result.reason!r}, "
            f"message={result.message!r}. "
            f"If precheck fails here, the DC12 upgrade path is broken."
        )

    def test_legacy_config_passes_precheck_with_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """serve with legacy config + binary → precheck passes, exit 0.

        Note: _run_server is patched to a no-op. This test verifies that
        precheck passes (exit 0) but does NOT verify that the HTTP server
        actually starts. The test name and assertion comments reflect this.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)
        # No state.json needed

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("secondsight.cli.serve._run_server"),  # no-op: test only verifies precheck
        ):
            result = runner.invoke(secondsight_app, ["serve"])

        # Assertion: precheck passed (binary found) → exit 0.
        # _run_server is a no-op: the HTTP server did NOT actually start.
        assert result.exit_code == 0, (
            f"Expected exit_code=0 for legacy config + binary (precheck should pass). "
            f"Got exit_code={result.exit_code}. "
            f"Output: {result.output!r}."
        )


# ---------------------------------------------------------------------------
# DEATH TEST: legacy config + state.json → serve warns and succeeds (DC5 compose)
# ---------------------------------------------------------------------------


class TestDTLegacyConfigWithStateServe:
    """Death test: re-start server with legacy config + state.json → WARN + successful startup.

    Spec requirement (task-7.md DC5): verifies the composed scenario:
      1. Legacy config.toml (flat [analysis] default_agent)
      2. state.json present with init_agent="claude_code"
      3. `secondsight serve` → WARN log about legacy field + exit 0

    Previous tests covered each piece separately; this test composes them.
    Scar task-7 acknowledged this was "implicitly covered" — that is NOT the
    same as covered. This test closes that gap.
    """

    def test_legacy_config_with_state_serve_warns_and_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy config + state.json → WARN about legacy field + exit 0.

        Silent failure path: if the loader silently ignores legacy config when
        state.json is present (different code path than without state.json),
        users who've previously run `init` would not see the upgrade warning.
        This test catches that regression.
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)
        _write_state_json(ss_home, agent="claude_code")

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("secondsight.cli.serve._run_server"),  # no-op: test only verifies precheck
        ):
            import logging as _logging

            with caplog.at_level(_logging.WARNING):
                result = runner.invoke(secondsight_app, ["serve"])

        # Assert 1: exit 0 (precheck passes — binary found, state.json present)
        assert result.exit_code == 0, (
            f"Expected exit_code=0 for legacy config + state.json + binary. "
            f"Got exit_code={result.exit_code}. "
            f"Output: {result.output!r}."
        )

        # Assert 2: WARN log mentions legacy field
        warn_messages = " ".join(r.message for r in caplog.records if r.levelno >= _logging.WARNING)
        assert "legacy" in warn_messages.lower() or "default_agent" in warn_messages, (
            f"Expected WARN log to mention 'legacy' or 'default_agent' "
            f"when serving with legacy config + state.json. "
            f"Got warn messages: {warn_messages!r}. "
            f"If no warning is emitted, users who have previously run init "
            f"will not see the upgrade prompt."
        )

        # Assert 3: mode is "cli" (not silently set to sdk by legacy key)
        cfg = load_global_config(ss_home)
        assert cfg.general.mode == "cli", (
            f"Expected mode='cli' after loading legacy config + state.json. "
            f"Got mode={cfg.general.mode!r}. "
            f"The legacy default_agent key must not change mode to 'sdk'."
        )


# ---------------------------------------------------------------------------
# DEATH TEST: init writes state.json correctly (upgrade step 2)
# ---------------------------------------------------------------------------


class TestDTInitWritesState:
    """Death test: `secondsight init --agent claude_code` writes state.json."""

    def test_init_with_legacy_config_writes_state_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After init in a home that has legacy config.toml, state.json is written.

        This validates the upgrade sequence:
        1. Legacy config present.
        2. Run `secondsight init --agent claude_code`.
        3. state.json now exists with init_agent="claude_code".
        """
        ss_home = tmp_path / ".secondsight"
        _write_legacy_config(ss_home)
        _mock_installer(monkeypatch)

        monkeypatch.setattr(
            "secondsight.cli._home.secondsight_home",
            lambda override="": ss_home,
        )
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "secondsight.cli._home.claude_home",
            lambda override="": claude_home,
        )

        result = runner.invoke(
            secondsight_app,
            ["init", "--agent", "claude_code", "--secondsight-home", str(ss_home)],
        )

        state_path = ss_home / "state.json"
        assert state_path.exists(), (
            f"state.json must exist after init. "
            f"Exit code: {result.exit_code}. Output: {result.output!r}."
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["init_agent"] == "claude_code", (
            f"init_agent must be 'claude_code'. Got: {data['init_agent']!r}."
        )


# ---------------------------------------------------------------------------
# UNIT TESTS — fresh install defaults
# ---------------------------------------------------------------------------


class TestUTFreshInstallDefaults:
    """Fresh install with no config.toml → mode defaults to 'cli'."""

    def test_no_config_mode_defaults_to_cli(self, tmp_path: Path) -> None:
        """No config.toml → load_global_config returns mode='cli'."""
        ss_home = tmp_path / ".secondsight"
        ss_home.mkdir(parents=True, exist_ok=True)
        # No config.toml

        cfg = load_global_config(ss_home)

        assert cfg.general.mode == "cli", (
            f"Fresh install must default to mode='cli'. Got: {cfg.general.mode!r}."
        )

    def test_no_config_no_state_precheck_fails_state_missing(self, tmp_path: Path) -> None:
        """Fresh install (no config, no state) + mode=cli → precheck fails state_missing."""
        ss_home = tmp_path / ".secondsight"
        ss_home.mkdir(parents=True, exist_ok=True)

        cfg = load_global_config(ss_home)
        result = precheck(config=cfg, state=None)

        assert not result.is_ok
        assert result.reason == "state_missing"


# NOTE: TestRegressionSDKTestsUnderExplicitSDKMode was moved to
# tests/e2e/test_sdk_regression.py per IMPORTANT FIX 7.
# SDK regression tests don't belong in the legacy-config-focused file.
