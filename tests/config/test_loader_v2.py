"""Death tests and unit tests for v2 loader behavior (Task 1 additions).

These tests cover:
- Legacy flat [analysis] default_agent detection (DC12)
- New [general] section parsing
- [providers.*] section parsing with ${VAR} interpolation
- New [analysis.cli] / [analysis.sdk] section parsing
- mode validation (only "cli" and "sdk" accepted)
- Empty string providers (Decision E1 — no implicit env fallback)

Death tests:
- DT-v2-loader-1: legacy config with flat [analysis] default_agent → WARN + mode defaults to "cli"
- DT-v2-loader-2: [general] mode = "invalid" → SecondSightConfigError raised
- DT-v2-loader-3: [providers.anthropic] ANTHROPIC_API_KEY = "${MY_KEY}" with MY_KEY unset → SecondSightConfigError
- DT-v2-loader-4: [providers.anthropic] ANTHROPIC_API_KEY = "" → resolves to "" (NOT env fallback, per E1)
- DT-v2-loader-5: fresh config (no config.toml) → general.mode defaults to "cli"
- DT-v2-loader-6: loader reads [analysis.cli.models] empty values as "" (Decision E5)

Unit tests:
- UT-v2-loader-1: [general] mode = "cli" → general.mode == "cli"
- UT-v2-loader-2: [general] mode = "sdk" → general.mode == "sdk"
- UT-v2-loader-3: [providers.anthropic] ANTHROPIC_API_KEY resolved from ${VAR}
- UT-v2-loader-4: [analysis.cli] default_agent = "codex" → analysis.cli.default_agent == "codex"
- UT-v2-loader-5: [analysis.sdk] primary_model set in TOML → analysis.sdk.primary_model matches
- UT-v2-loader-6: [analysis] timeout_seconds set in TOML → analysis.timeout_seconds matches
- UT-v2-loader-7: [analysis.cli.models] partial model override
- UT-v2-loader-8: no [providers] section → ProvidersConfig defaults (all empty)
- UT-v2-loader-9: no [general] section → GeneralConfig defaults (mode="cli")
- UT-v2-loader-10: [analysis] timeout_seconds absent → default 300
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Death tests — must fail before implementation (red phase)
# ---------------------------------------------------------------------------


class TestDTV2Loader1LegacyFlatAnalysis:
    """DT-v2-loader-1: Legacy flat [analysis] default_agent = "claude_code" triggers WARN.

    DC12: Legacy config has flat [analysis] default_agent (pre-toggle schema).
    Loader must WARN containing substring "legacy [analysis] default_agent"
    and NOT raise. Resolved config must have general.mode == "cli" (built-in default).

    Silent failure path: if legacy config silently changes mode or default_agent
    without warning, users who have old configs will get unexpected behavior
    with no indication why.
    """

    def test_legacy_flat_default_agent_warns_and_does_not_raise(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        # Flat [analysis] default_agent — the OLD pre-toggle schema
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "claude_code"\n',
        )

        # Must NOT raise
        cfg = load_global_config(home)

        # POSITIVE: assert the bridge delivered SOMETHING before checking specifics.
        # If caplog.records is empty here, the loguru->caplog bridge is broken --
        # see tests/test_conftest_bridge.py which provides an actionable failure message.
        assert len(caplog.records) > 0, (
            "no log records captured at all; loguru->caplog bridge may be broken. "
            "Run tests/test_conftest_bridge.py::test_loguru_caplog_bridge_is_wired "
            "to diagnose. Do NOT skip this assert -- fix the bridge."
        )

        # Must WARN with the specific substring
        warn_messages = [r.message for r in caplog.records if r.levelno >= 30]
        matching = [m for m in warn_messages if "legacy [analysis] default_agent" in str(m)]
        assert matching, (
            f"Expected at least one warning containing 'legacy [analysis] default_agent'. "
            f"Got warnings: {warn_messages}. "
            f"Without this warning, users with legacy configs silently get unexpected behavior."
        )

        # general.mode must use built-in default "cli"
        assert cfg.general.mode == "cli", (
            f"Legacy config must not override general.mode. Got {cfg.general.mode!r}."
        )

    def test_legacy_flat_default_agent_mode_not_overridden(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"\n',
        )
        cfg = load_global_config(home)
        # The legacy field is IGNORED; it must NOT affect new analysis.cli.default_agent
        # (new analysis config uses "auto" by default or whatever [analysis.cli].default_agent says)
        assert cfg.general.mode == "cli"


class TestDTV2Loader2InvalidModeRaises:
    """DT-v2-loader-2: [general] mode = "invalid" must raise SecondSightConfigError.

    DC from acceptance.yaml: only "cli" and "sdk" are valid modes.
    Silent failure path: if invalid mode is accepted, mode-aware dispatch falls into
    an undefined branch that may silently use the wrong dispatch path.
    """

    def test_invalid_mode_raises(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import SecondSightConfigError

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[general]\nmode = "invalid"\n',
        )

        with pytest.raises(SecondSightConfigError) as exc_info:
            load_global_config(home)

        assert "invalid" in str(exc_info.value).lower() or "mode" in str(exc_info.value).lower(), (
            "Error message should mention the invalid mode value or 'mode' field."
        )

    def test_mode_sdk_does_not_raise(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[general]\nmode = "sdk"\n',
        )
        cfg = load_global_config(home)
        assert cfg.general.mode == "sdk"

    def test_mode_cli_does_not_raise(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[general]\nmode = "cli"\n',
        )
        cfg = load_global_config(home)
        assert cfg.general.mode == "cli"


class TestDTV2Loader3UnsetProviderKeyRaises:
    """DT-v2-loader-3: ${MY_KEY} with MY_KEY unset → SecondSightConfigError.

    This is the existing interpolation behavior — the test confirms it propagates
    through the new [providers.*] section parsing correctly.
    """

    def test_unset_var_in_providers_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import SecondSightConfigError

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[providers.anthropic]\nANTHROPIC_API_KEY = "${MY_TOTALLY_ABSENT_KEY}"\n',
        )
        monkeypatch.delenv("MY_TOTALLY_ABSENT_KEY", raising=False)

        with pytest.raises(SecondSightConfigError):
            load_global_config(home)


class TestDTV2Loader4EmptyProviderKeyNoEnvFallback:
    """DT-v2-loader-4: Empty string in providers resolves to "" (NOT env lookup, per E1).

    DC7: User has $ANTHROPIC_API_KEY in env, config has ANTHROPIC_API_KEY = "" →
    loader resolves empty string to empty string (no implicit fallback).

    Silent failure path: if loader falls back to env when TOML is "", the user
    who explicitly clears the key to disable SDK mode will still have the key
    injected silently, bypassing the design intent.
    """

    def test_empty_provider_key_resolves_to_empty_not_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[providers.anthropic]\nANTHROPIC_API_KEY = ""\n',
        )
        # Set env var — it must NOT be used when TOML value is ""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")

        cfg = load_global_config(home)

        assert cfg.providers.anthropic.ANTHROPIC_API_KEY == "", (
            "ANTHROPIC_API_KEY = '' in TOML must resolve to '' per Decision E1 — "
            "no implicit env fallback. "
            f"Got {cfg.providers.anthropic.ANTHROPIC_API_KEY!r} instead."
        )


class TestDTV2Loader5FreshConfigDefaultMode:
    """DT-v2-loader-5: No config.toml → general.mode defaults to "cli".

    Acceptance: "Upgrade — fresh install with no config.toml gets default mode=cli".
    Silent failure: if missing config produces SDK mode, fresh installs without API
    keys fail immediately on first analysis attempt with cryptic auth errors.
    """

    def test_no_config_toml_defaults_to_cli(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".no-such-dir"
        cfg = load_global_config(home)

        assert cfg.general.mode == "cli", (
            f"No config.toml must default to mode='cli'. Got {cfg.general.mode!r}."
        )

    def test_no_config_toml_defaults_to_general_config_instance(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import GeneralConfig

        home = tmp_path / ".no-such-dir"
        cfg = load_global_config(home)

        assert isinstance(cfg.general, GeneralConfig)


class TestDTV2Loader6CLIModelsEmptyAllowed:
    """DT-v2-loader-6: [analysis.cli.models] empty values must be preserved as "".

    Decision E5: empty model = "let coding agent use its own default model".
    Silent failure: if empty is rejected or coerced to a default model name,
    the user can't opt-out of model override — coding agent's own model selection
    is silently bypassed.
    """

    def test_empty_cli_model_preserved(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis.cli.models]\nclaude_code = ""\ncodex = ""\nopencode = ""\n',
        )
        cfg = load_global_config(home)

        assert cfg.analysis.cli.models.claude_code == ""
        assert cfg.analysis.cli.models.codex == ""
        assert cfg.analysis.cli.models.opencode == ""

    def test_nonempty_cli_model_preserved(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis.cli.models]\nclaude_code = "claude-opus-4-5"\n',
        )
        cfg = load_global_config(home)
        assert cfg.analysis.cli.models.claude_code == "claude-opus-4-5"


class TestDTV2Loader7RetryPolicyValidation:
    """DT-v2-loader-7: [analysis.retry] invalid values are rejected at load time."""

    def test_negative_output_repair_max_attempts_raises(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import SecondSightConfigError

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            "[analysis.retry]\noutput_repair_max_attempts = -1\n",
        )

        with pytest.raises(SecondSightConfigError, match="output_repair_max_attempts"):
            load_global_config(home)

    def test_output_repair_max_attempts_above_hard_cap_raises(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import SecondSightConfigError

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            "[analysis.retry]\noutput_repair_max_attempts = 6\n",
        )

        with pytest.raises(SecondSightConfigError, match="output_repair_max_attempts"):
            load_global_config(home)


# ---------------------------------------------------------------------------
# Unit tests (happy path)
# ---------------------------------------------------------------------------


class TestUTV2Loader1GeneralModeFromToml:
    """UT-v2-loader-1: [general] mode = "cli" is parsed correctly."""

    def test_mode_cli_from_toml(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(home / "config.toml", '[general]\nmode = "cli"\n')
        cfg = load_global_config(home)
        assert cfg.general.mode == "cli"


class TestUTV2Loader2GeneralModeSDK:
    """UT-v2-loader-2: [general] mode = "sdk" parsed correctly."""

    def test_mode_sdk_from_toml(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(home / "config.toml", '[general]\nmode = "sdk"\n')
        cfg = load_global_config(home)
        assert cfg.general.mode == "sdk"


class TestUTV2Loader3ProviderVarInterpolation:
    """UT-v2-loader-3: [providers.anthropic] ANTHROPIC_API_KEY resolved from ${VAR}."""

    def test_var_interpolated_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[providers.anthropic]\nANTHROPIC_API_KEY = "${TEST_ANTHR_KEY}"\n',
        )
        monkeypatch.setenv("TEST_ANTHR_KEY", "sk-test-value")

        cfg = load_global_config(home)
        assert cfg.providers.anthropic.ANTHROPIC_API_KEY == "sk-test-value"


class TestUTV2Loader4CLIDefaultAgent:
    """UT-v2-loader-4: [analysis.cli] default_agent parsed from TOML."""

    def test_cli_default_agent_from_toml(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis.cli]\ndefault_agent = "codex"\n',
        )
        cfg = load_global_config(home)
        assert cfg.analysis.cli.default_agent == "codex"


class TestUTV2Loader5SDKPrimaryModel:
    """UT-v2-loader-5: [analysis.sdk] primary_model from TOML."""

    def test_sdk_primary_model_from_toml(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis.sdk]\nprimary_model = "claude-opus-4-5"\nfallback_model = "gpt-4o-mini"\n',
        )
        cfg = load_global_config(home)
        assert cfg.analysis.sdk.primary_model == "claude-opus-4-5"
        assert cfg.analysis.sdk.fallback_model == "gpt-4o-mini"


class TestUTV2Loader6AnalysisTimeout:
    """UT-v2-loader-6: [analysis] timeout_seconds from TOML."""

    def test_timeout_seconds_from_toml(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            "[analysis]\ntimeout_seconds = 600\n",
        )
        cfg = load_global_config(home)
        assert cfg.analysis.timeout_seconds == 600


class TestUTV2Loader7CLIModelsPartial:
    """UT-v2-loader-7: [analysis.cli.models] partial override — only set field changes."""

    def test_partial_model_override(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis.cli.models]\nclaude_code = "claude-sonnet-4-6"\n',
        )
        cfg = load_global_config(home)
        assert cfg.analysis.cli.models.claude_code == "claude-sonnet-4-6"
        assert cfg.analysis.cli.models.codex == ""  # unset = ""
        assert cfg.analysis.cli.models.opencode == ""  # unset = ""


class TestUTV2Loader8NoProvidersSection:
    """UT-v2-loader-8: No [providers] section → ProvidersConfig defaults (all empty keys)."""

    def test_no_providers_uses_defaults(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import ProvidersConfig

        home = tmp_path / ".secondsight"
        _write_toml(home / "config.toml", '[general]\nmode = "cli"\n')
        cfg = load_global_config(home)

        assert isinstance(cfg.providers, ProvidersConfig)
        assert cfg.providers.anthropic.ANTHROPIC_API_KEY == ""
        assert cfg.providers.openai.OPENAI_API_KEY == ""


class TestUTV2Loader9NoGeneralSection:
    """UT-v2-loader-9: No [general] section → GeneralConfig defaults."""

    def test_no_general_uses_defaults(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import GeneralConfig

        home = tmp_path / ".secondsight"
        _write_toml(home / "config.toml", "[retention]\nraw_traces_ttl_days = 30\n")
        cfg = load_global_config(home)

        assert isinstance(cfg.general, GeneralConfig)
        assert cfg.general.mode == "cli"


class TestUTV2Loader10TimeoutDefault:
    """UT-v2-loader-10: [analysis] timeout_seconds absent → default 300."""

    def test_default_timeout_when_absent(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(home / "config.toml", '[general]\nmode = "cli"\n')
        cfg = load_global_config(home)
        assert cfg.analysis.timeout_seconds == 300


class TestUTV2Loader11ProviderCustomSection:
    """UT-v2-loader-11: [providers.custom] section with base_url parsed correctly.

    This verifies tomllib handles [providers.custom] with base_url field
    (assumption from task: tomllib parses this correctly — TOML quoting is a known footgun).
    """

    def test_custom_provider_base_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[providers.custom]\nAPI_KEY = ""\nbase_url = "https://api.example.com/v1"\n',
        )
        cfg = load_global_config(home)

        assert cfg.providers.custom.base_url == "https://api.example.com/v1"
        assert cfg.providers.custom.API_KEY == ""


class TestUTV2Loader12RetrySection:
    """UT-v2-loader-12: [analysis.retry] section is parsed into AnalysisConfig.retry."""

    def test_retry_section_from_toml(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            "[analysis.retry]\nenabled = false\noutput_repair_max_attempts = 4\nfeedback_max_chars = 900\n",
        )

        cfg = load_global_config(home)
        assert cfg.analysis.retry.enabled is False
        assert cfg.analysis.retry.output_repair_max_attempts == 4
        assert cfg.analysis.retry.feedback_max_chars == 900


# ---------------------------------------------------------------------------
# Death tests for re-review round — CRITICAL FIX 1, 2 and IMPORTANT FIX 3
# ---------------------------------------------------------------------------


class TestDTCriticalFix1StateStatusRendered:
    """DT-critical-fix-1: _render_text must surface state_status for all outcome variants.

    Silent failure path: if state_status is dropped, a user running `secondsight init`
    sees "[cyan]Installed[/cyan]" even when state.json write silently failed (disk full /
    permission denied). Task 6's "auto" resolution then finds no state.json and falls
    through to surprising defaults — with no signal to the user.

    These death tests verify that _render_text prints each status variant,
    using Rich Console capture. They are written BEFORE the fix — they will
    FAIL until _render_text is updated to render state_status.
    """

    def _run_render_text(self, state_status: str, dry_run: bool = False) -> str:
        """Run _render_text with a minimal summary dict and capture output."""
        from io import StringIO

        from rich.console import Console

        from secondsight.cli.init import _render_text

        buf = StringIO()
        # Temporarily swap the module's _console to capture output
        import secondsight.cli.init as init_mod

        old_console = init_mod._console
        init_mod._console = Console(file=buf, highlight=False, markup=False)
        try:
            summary = {
                "agent": "claude_code",
                "agent_home": "/tmp/.claude",
                "dry_run": dry_run,
                "hook_dir": "/tmp/.claude/hooks",
                "registration_path": "/tmp/.claude/settings.json",
                "scripts_source": "/tmp/hooks",
                "scripts_copied": [],
                "scripts_skipped_identical": [],
                "settings_actions": {},
                "settings_file_existed": True,
                "foreign_secondsight_paths": [],
                "config_status": "",
                "state_status": state_status,
                "secondsight_home": "/tmp/.secondsight",
            }
            _render_text(summary)
        finally:
            init_mod._console = old_console
        return buf.getvalue()

    def test_state_status_written_appears_in_output(self) -> None:
        """state.json written must appear in text output."""
        output = self._run_render_text("state.json written (init_agent='claude_code')")
        assert "state.json" in output, (
            f"state_status 'written' must appear in _render_text output. Got:\n{output}"
        )

    def test_state_status_failed_appears_in_output(self) -> None:
        """state.json write failed must appear prominently in text output.

        This is the critical path: silent write failure is invisible if not rendered.
        """
        output = self._run_render_text(
            "state.json write failed: [Errno 28] No space left on device"
        )
        assert "state.json" in output, (
            f"state_status 'failed' must appear in _render_text output. Got:\n{output}"
        )
        # The failure message text must be in output
        assert "write failed" in output or "No space left" in output, (
            f"Failure details must be visible. Got:\n{output}"
        )

    def test_state_status_unchanged_appears_in_output(self) -> None:
        """state.json unchanged (overwrite declined) must appear in text output."""
        output = self._run_render_text("state.json unchanged (kept init_agent='codex')")
        assert "state.json" in output, (
            f"state_status 'unchanged' must appear in _render_text output. Got:\n{output}"
        )

    def test_state_status_dry_run_appears_in_output(self) -> None:
        """state.json dry-run status must appear in text output."""
        output = self._run_render_text(
            "dry-run: would write state.json (init_agent='claude_code')", dry_run=True
        )
        assert "state.json" in output, (
            f"state_status dry-run must appear in _render_text output. Got:\n{output}"
        )


class TestDTCriticalFix2LegacyDefaultAgentBothBuildersAgree:
    """DT-critical-fix-2: legacy flat [analysis] default_agent must be ignored by BOTH builders.

    DC12 spec language is warn-and-ignore, not warn-but-keep-honoring. When a legacy config
    has [analysis] default_agent = "codex" with no [analysis.cli] section:
    - _build_analysis_config: already ignores it → analysis.cli.default_agent = "auto"
    - _build_global_analysis_config: MUST ALSO ignore it after fix

    Silent failure path: without this fix, cfg.analysis_global.default_agent returns "codex"
    while cfg.analysis.cli.default_agent returns "auto". runtime.py reads analysis_global
    and dispatches to "codex" — but DC12 told the user their value was IGNORED.

    The two fields must NOT diverge for the same input config.
    After fix, both should equal BUILTIN_DEFAULT_AGENT ("claude_code").
    """

    def test_legacy_flat_analysis_global_and_analysis_cli_agree(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Both cfg.analysis_global.default_agent and cfg.analysis.cli.default_agent
        must equal BUILTIN_DEFAULT_AGENT when legacy flat key is present and no nested cli section."""
        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import BUILTIN_DEFAULT_AGENT

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"\n',
        )
        cfg = load_global_config(home)

        assert cfg.analysis.cli.default_agent == BUILTIN_DEFAULT_AGENT, (
            f"analysis.cli.default_agent must equal BUILTIN_DEFAULT_AGENT={BUILTIN_DEFAULT_AGENT!r} "
            f"after warn-and-ignore of legacy flat key. "
            f"Got {cfg.analysis.cli.default_agent!r}."
        )
        assert cfg.analysis_global.default_agent == BUILTIN_DEFAULT_AGENT, (
            f"analysis_global.default_agent must equal BUILTIN_DEFAULT_AGENT={BUILTIN_DEFAULT_AGENT!r} "
            f"after warn-and-ignore of legacy flat key. "
            f"Got {cfg.analysis_global.default_agent!r}. "
            f"Without this fix, runtime.py dispatches to the 'ignored' legacy agent."
        )

    def test_legacy_flat_both_agree_equal_each_other(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The two fields must produce the same effective agent — no divergence."""
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"\n',
        )
        cfg = load_global_config(home)

        assert cfg.analysis_global.default_agent == cfg.analysis.cli.default_agent, (
            f"analysis_global.default_agent={cfg.analysis_global.default_agent!r} and "
            f"analysis.cli.default_agent={cfg.analysis.cli.default_agent!r} must not diverge "
            f"for the same legacy config input. "
            f"A user told 'legacy key is ignored' must see the same agent from both paths."
        )


class TestDTImportantFix3DC12WarnCondition:
    """DT-important-fix-3: DC12 warn must fire only when flat key is present WITHOUT nested cli.

    Problem: current condition `if "default_agent" in analysis_section` fires even when
    the user has BOTH flat and [analysis.cli] (migration state). This causes alert fatigue.

    Death tests:
    - flat alone → MUST warn (existing behavior preserved)
    - flat + nested → MUST NOT warn (new behavior after fix)

    Silent failure path: if warn fires on migration configs, users learn to ignore DC12
    warnings, and real legacy-only configs also stop being noticed.
    """

    def test_flat_only_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Flat [analysis] default_agent with NO [analysis.cli] → MUST warn."""
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"\n',
        )
        load_global_config(home)

        warn_messages = [r.message for r in caplog.records if r.levelno >= 30]
        matching = [m for m in warn_messages if "legacy [analysis] default_agent" in str(m)]
        assert matching, (
            f"Flat-only legacy config must trigger DC12 warning. Got warnings: {warn_messages}"
        )

    def test_flat_plus_nested_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Flat [analysis] default_agent WITH [analysis.cli] present → MUST NOT warn.

        This is the migration state: user has the new key, flat key is a leftover.
        Nested key takes precedence. No warning.
        """
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"\n\n[analysis.cli]\ndefault_agent = "claude_code"\n',
        )
        load_global_config(home)

        warn_messages = [r.message for r in caplog.records if r.levelno >= 30]
        matching = [m for m in warn_messages if "legacy [analysis] default_agent" in str(m)]
        assert not matching, (
            f"Flat + nested migration config must NOT trigger DC12 warning. "
            f"Got unexpected warnings: {matching}"
        )

    def test_flat_plus_nested_uses_nested_value(self, tmp_path: Path) -> None:
        """When both flat and nested are present, the nested [analysis.cli] value wins."""
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"\n\n[analysis.cli]\ndefault_agent = "claude_code"\n',
        )
        cfg = load_global_config(home)
        assert cfg.analysis.cli.default_agent == "claude_code", (
            f"Nested [analysis.cli].default_agent must win over flat key. "
            f"Got {cfg.analysis.cli.default_agent!r}."
        )


class TestUTV2LoaderFeedbackConfig:
    """Loader resolves [feedback] into SecondSightConfig.feedback."""

    def test_global_feedback_config_resolves(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_global_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            "[feedback]\nconvention_injection_budget = 777\nconvention_top_n = 8\n",
        )

        cfg = load_global_config(home)

        assert cfg.feedback.convention_injection_budget == 777
        assert cfg.feedback.convention_top_n == 8

    def test_project_feedback_overrides_global_per_field(self, tmp_path: Path) -> None:
        from secondsight.config.loader import load_project_config

        home = tmp_path / ".secondsight"
        _write_toml(
            home / "config.toml",
            "[feedback]\nconvention_injection_budget = 777\nconvention_top_n = 8\n",
        )
        _write_toml(
            home / "projects" / "proj-1" / "config.toml",
            "[feedback]\nconvention_injection_budget = 111\n",
        )

        cfg = load_project_config(home, "proj-1")

        assert cfg.feedback.convention_injection_budget == 111
        assert cfg.feedback.convention_top_n == 8
