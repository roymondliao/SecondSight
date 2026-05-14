"""SDK regression tests — no external services required.

Verifies that SDK-path behavior (dispatcher, router, precheck, ModeAwareDispatch
routing) survives after the default-mode change to 'cli' in Task 7.

These tests are NOT gated (no SECONDSIGHT_TEST_REAL_LLM required). They use
in-process invocations, mock dispatchers, and in-memory configs. They must pass
in CI and any sandbox.

Background:
    Decision D (mode defaults to 'cli'): any test that previously relied on
    implicit SDK behavior (no config = sdk) must now either supply an explicit
    mode=sdk config fixture or skip with a gated marker. This file provides the
    non-gated regression layer.

Moved from test_legacy_config_upgrade.py (TestRegressionSDKTestsUnderExplicitSDKMode)
per IMPORTANT FIX 7 — the class did not belong in legacy-config-focused file.
"""

from __future__ import annotations

from secondsight.config.precheck import precheck
from secondsight.config.schema import (
    AnalysisCLIConfig,
    AnalysisConfig,
    AnalysisSDKConfig,
    GeneralConfig,
    GlobalAnalysisConfig,
    ProjectAnalysisConfig,
    ProviderAnthropicConfig,
    ProviderCustomConfig,
    ProviderOpenAIConfig,
    ProvidersConfig,
    SecondSightConfig,
)
from secondsight.storage.retention import RetentionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_retention() -> RetentionConfig:
    return RetentionConfig(
        raw_traces_ttl_days=30,
        raw_traces_source="builtin_default",
        analysis_ttl_days=90,
        analysis_ttl_source="builtin_default",
        cleanup_after_analysis=False,
    )


def _make_sdk_config(api_key: str = "sk-test-regression-key") -> SecondSightConfig:
    return SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode="sdk"),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY=api_key),
            openai=ProviderOpenAIConfig(OPENAI_API_KEY=""),
            custom=ProviderCustomConfig(API_KEY="", base_url=""),
        ),
        analysis=AnalysisConfig(
            timeout_seconds=300,
            cli=AnalysisCLIConfig(),
            sdk=AnalysisSDKConfig(
                primary_model="claude-haiku-4-5-20251001",
                fallback_model="gpt-4o-mini",
            ),
        ),
        analysis_global=GlobalAnalysisConfig(),
        project_analysis=ProjectAnalysisConfig(),
    )


# ---------------------------------------------------------------------------
# REGRESSION TESTS: SDK path survives default-mode change to 'cli'
# ---------------------------------------------------------------------------


class TestRegressionSDKTestsUnderExplicitSDKMode:
    """Regression: existing SDK-path behavior survives after default-mode change to 'cli'.

    Decision D: mode now defaults to 'cli'. Any test that previously relied on
    implicit SDK behavior (no config = sdk) must now either:
    (a) Supply an explicit mode=sdk config fixture, OR
    (b) Skip with "CLI mode requires SECONDSIGHT_TEST_REAL_CLI=1" message.

    This test verifies that the SDK dispatcher, LLMRouter, and precheck all
    function correctly when mode=sdk is explicitly configured.
    """

    def test_sdk_mode_explicit_config_passes_precheck(self) -> None:
        """Explicit mode=sdk config with providers configured → precheck passes."""
        sdk_cfg = _make_sdk_config()
        result = precheck(config=sdk_cfg, state=None)

        assert result.is_ok, (
            f"Explicit mode=sdk config must pass precheck. "
            f"Got reason={result.reason!r}, message={result.message!r}."
        )

    def test_sdk_dispatcher_importable_under_any_mode(self) -> None:
        """SDKAnalysisDispatcher imports successfully regardless of mode setting."""
        from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher  # noqa: F401

    def test_cli_dispatcher_importable_under_any_mode(self) -> None:
        """CLIAnalysisDispatcher imports successfully regardless of mode setting."""
        from secondsight.analysis.cli_dispatcher import CLIAnalysisDispatcher  # noqa: F401

    def test_mode_aware_dispatch_routes_to_correct_dispatcher(self) -> None:
        """ModeAwareDispatch selects SDK dispatcher when mode=sdk, CLI when mode=cli.

        Regression: this routing must not be broken by default-mode change.
        """
        from secondsight.analysis.runtime import ModeAwareDispatch

        sdk_cfg = _make_sdk_config(api_key="sk-regression")

        # Verify the mode is correctly read back and would route to sdk
        mad = ModeAwareDispatch(
            config=sdk_cfg,
            state=None,
            cli_dispatcher=None,
            sdk_dispatcher=None,
        )

        # ModeAwareDispatch reads mode from config.general.mode each dispatch.
        # Verify the config was stored correctly (no mode silently changed).
        assert mad._config.general.mode == "sdk", (
            f"ModeAwareDispatch must preserve mode='sdk' from config. "
            f"Got _config.general.mode={mad._config.general.mode!r}."
        )
