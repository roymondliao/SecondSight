"""Death tests and unit tests for the new v2 config schema dataclasses.

Death tests target silent failure paths — they must fail (ImportError, AttributeError,
or AssertionError) BEFORE implementation to confirm we are testing real behaviour.

DC = Death Case (from 2-plan.md §9 and task dispatch)

Death tests:
- DT-v2-schema-1: GeneralConfig importable from config.schema (not yet — should ImportError before impl)
- DT-v2-schema-2: AnalysisConfig (new, wrapping cli+sdk) importable from config.schema
- DT-v2-schema-3: GeneralConfig defaults: mode="cli", log_level="info"
- DT-v2-schema-4: AnalysisCLIConfig and AnalysisSDKConfig exist with correct defaults
- DT-v2-schema-5: SecondSightConfig has new fields: general, providers, analysis (AnalysisConfig type)
- DT-v2-schema-6: AnalysisConfig (new) is NOT the same as analysis.config.AnalysisConfig (different class)
- DT-v2-schema-7: GlobalAnalysisConfig still importable (backward compat preserved)

Unit tests (happy path):
- UT-v2-schema-1: GeneralConfig defaults
- UT-v2-schema-2: ProviderAnthropicConfig defaults
- UT-v2-schema-3: AnalysisCLIModelsConfig defaults
- UT-v2-schema-4: AnalysisCLIConfig defaults (default_agent="auto")
- UT-v2-schema-5: AnalysisSDKConfig defaults
- UT-v2-schema-6: AnalysisConfig defaults
- UT-v2-schema-7: ProvidersConfig defaults
- UT-v2-schema-8: SecondSightConfig fields include general, providers, analysis
- UT-v2-schema-9: All new dataclasses are frozen
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Death tests — MUST fail before implementation (red phase)
# ---------------------------------------------------------------------------


class TestDTV2Schema1GeneralConfigExists:
    """DT-v2-schema-1: GeneralConfig must be importable from config.schema.

    Silent failure path: if GeneralConfig is missing, callers silently get
    AttributeError at runtime when accessing config.general.mode, only discovered
    when mode-aware dispatch is triggered.
    """

    def test_general_config_importable(self) -> None:
        from secondsight.config.schema import GeneralConfig  # noqa: F401

    def test_general_config_has_mode_field(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import GeneralConfig

        field_names = {f.name for f in fields(GeneralConfig)}
        assert "mode" in field_names, (
            "GeneralConfig must have a 'mode' field. Without it, mode-aware dispatch "
            "silently falls back to hardcoded defaults, making the toggle non-functional."
        )

    def test_general_config_has_log_level_field(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import GeneralConfig

        field_names = {f.name for f in fields(GeneralConfig)}
        assert "log_level" in field_names


class TestDTV2Schema2AnalysisConfigExists:
    """DT-v2-schema-2: New AnalysisConfig (aggregate of cli+sdk) importable from config.schema.

    NOTE: This conflicts with the old test_schema.py::TestDTSchema4::test_analysis_config_NOT_in_schema
    which asserted AnalysisConfig must NOT be in schema. The old test was written before
    this task. Task 1 intentionally introduces AnalysisConfig to schema.py as a NEW class
    that is distinct from analysis.config.AnalysisConfig (which remains for per-project TOML loading).
    The old guard test in test_schema.py must be updated after implementation.
    """

    def test_analysis_config_importable_from_schema(self) -> None:
        from secondsight.config.schema import AnalysisConfig  # noqa: F401

    def test_analysis_cli_config_importable(self) -> None:
        from secondsight.config.schema import AnalysisCLIConfig  # noqa: F401

    def test_analysis_sdk_config_importable(self) -> None:
        from secondsight.config.schema import AnalysisSDKConfig  # noqa: F401

    def test_analysis_cli_models_config_importable(self) -> None:
        from secondsight.config.schema import AnalysisCLIModelsConfig  # noqa: F401


class TestDTV2Schema3GeneralConfigDefaults:
    """DT-v2-schema-3: GeneralConfig must default to mode="cli", log_level="info".

    DC from Decision D: mode default = "cli". If the default is wrong, all users
    who don't explicitly set mode get SDK mode (which requires API keys), silently
    failing on every analysis with authentication errors.
    """

    def test_default_mode_is_cli(self) -> None:
        from secondsight.config.schema import GeneralConfig

        cfg = GeneralConfig()
        assert cfg.mode == "cli", (
            f"GeneralConfig.mode default must be 'cli' per Decision D. Got {cfg.mode!r}. "
            "Wrong default causes all no-config users to get SDK mode, failing silently "
            "with auth errors on every analysis run."
        )

    def test_default_log_level_is_info(self) -> None:
        from secondsight.config.schema import GeneralConfig

        cfg = GeneralConfig()
        assert cfg.log_level == "info"


class TestDTV2Schema4CLIAndSDKConfigDefaults:
    """DT-v2-schema-4: AnalysisCLIConfig and AnalysisSDKConfig have correct defaults."""

    def test_cli_config_default_agent_is_auto(self) -> None:
        from secondsight.config.schema import AnalysisCLIConfig

        cfg = AnalysisCLIConfig()
        assert cfg.default_agent == "auto", (
            "AnalysisCLIConfig.default_agent must default to 'auto'. "
            "Wrong default bypasses state.json resolution, using hardcoded agent."
        )

    def test_sdk_config_has_primary_model(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import AnalysisSDKConfig

        field_names = {f.name for f in fields(AnalysisSDKConfig)}
        assert "primary_model" in field_names

    def test_sdk_config_has_fallback_model(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import AnalysisSDKConfig

        field_names = {f.name for f in fields(AnalysisSDKConfig)}
        assert "fallback_model" in field_names

    def test_cli_models_config_has_expected_fields(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import AnalysisCLIModelsConfig

        field_names = {f.name for f in fields(AnalysisCLIModelsConfig)}
        assert "claude_code" in field_names
        assert "codex" in field_names
        assert "opencode" in field_names


class TestDTV2Schema5SecondSightConfigNewFields:
    """DT-v2-schema-5: SecondSightConfig must have 'general', 'providers', 'analysis' fields.

    Without these, config.general.mode access raises AttributeError at dispatch time —
    silent failure discovered only when mode-aware dispatch is exercised.
    """

    def test_secondsight_config_has_general_field(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import SecondSightConfig

        field_names = {f.name for f in fields(SecondSightConfig)}
        assert "general" in field_names, (
            "SecondSightConfig must have 'general' field. Without it, "
            "mode-aware dispatch has no way to read [general].mode."
        )

    def test_secondsight_config_has_providers_field(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import SecondSightConfig

        field_names = {f.name for f in fields(SecondSightConfig)}
        assert "providers" in field_names

    def test_secondsight_config_general_is_general_config_type(self) -> None:
        from secondsight.config.schema import GeneralConfig, SecondSightConfig

        cfg = SecondSightConfig.__dataclass_fields__["general"]
        # type annotation may be string or actual class
        assert "GeneralConfig" in str(cfg.type) or cfg.type is GeneralConfig


class TestDTV2Schema6AnalysisConfigIsDistinctClass:
    """DT-v2-schema-6: New schema.AnalysisConfig != analysis.config.AnalysisConfig.

    The new config.schema.AnalysisConfig is a pure config aggregate (cli + sdk sections).
    The old analysis.config.AnalysisConfig is the per-project TOML reader with .load().
    They must be DIFFERENT classes. If they were the same, the schema import would
    create a circular dependency (schema -> analysis -> schema).
    """

    def test_new_analysis_config_different_from_old(self) -> None:
        from secondsight.analysis.config import AnalysisConfig as OldAnalysisConfig
        from secondsight.config.schema import AnalysisConfig as NewAnalysisConfig

        assert OldAnalysisConfig is not NewAnalysisConfig, (
            "schema.AnalysisConfig (new, cli+sdk aggregate) must be a DIFFERENT class "
            "from analysis.config.AnalysisConfig (old, per-project TOML reader). "
            "If they were the same, schema.py would import from analysis.config, "
            "creating a circular dependency."
        )

    def test_new_analysis_config_has_no_load_method(self) -> None:
        """New AnalysisConfig in schema.py should NOT have a .load() classmethod.

        .load() belongs to analysis.config.AnalysisConfig (per-project TOML reader).
        The schema.AnalysisConfig is a pure dataclass, no TOML I/O.
        """
        from secondsight.config.schema import AnalysisConfig

        assert not hasattr(AnalysisConfig, "load"), (
            "schema.AnalysisConfig must NOT have .load(). "
            "TOML reading belongs in analysis.config.AnalysisConfig."
        )


class TestDTV2Schema7BackwardCompatGlobalAnalysisConfig:
    """DT-v2-schema-7: GlobalAnalysisConfig must still be importable from config.schema.

    GlobalAnalysisConfig is used by analysis.runtime and other modules for the
    old 'default_agent + models' flat schema. Removing it breaks backward compat.
    """

    def test_global_analysis_config_still_importable(self) -> None:
        from secondsight.config.schema import GlobalAnalysisConfig  # noqa: F401

    def test_global_analysis_config_class_identity_preserved(self) -> None:
        """analysis.config.GlobalAnalysisConfig must still be same class as schema version."""
        from secondsight.analysis.config import GlobalAnalysisConfig as AnalysisGAC
        from secondsight.config.schema import GlobalAnalysisConfig as SchemaGAC

        assert AnalysisGAC is SchemaGAC


# ---------------------------------------------------------------------------
# Unit tests (happy path) — verify correct defaults after implementation
# ---------------------------------------------------------------------------


class TestUTV2Schema1GeneralConfigDefaults:
    """UT-v2-schema-1: GeneralConfig defaults and frozen behavior."""

    def test_defaults(self) -> None:
        from secondsight.config.schema import GeneralConfig

        cfg = GeneralConfig()
        assert cfg.mode == "cli"
        assert cfg.log_level == "info"

    def test_frozen(self) -> None:
        from secondsight.config.schema import GeneralConfig

        cfg = GeneralConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.mode = "sdk"  # type: ignore[misc]

    def test_custom_mode(self) -> None:
        from secondsight.config.schema import GeneralConfig

        cfg = GeneralConfig(mode="sdk")
        assert cfg.mode == "sdk"


class TestUTV2Schema2ProviderAnthropicConfigDefaults:
    """UT-v2-schema-2: ProviderAnthropicConfig defaults to empty string (unset)."""

    def test_defaults_empty_string(self) -> None:
        from secondsight.config.schema import ProviderAnthropicConfig

        cfg = ProviderAnthropicConfig()
        assert cfg.ANTHROPIC_API_KEY == "", (
            "ProviderAnthropicConfig.ANTHROPIC_API_KEY must default to '' (not None, "
            "not env lookup). Per Decision E1: no implicit env fallback."
        )

    def test_frozen(self) -> None:
        from secondsight.config.schema import ProviderAnthropicConfig

        cfg = ProviderAnthropicConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.ANTHROPIC_API_KEY = "sk-test"  # type: ignore[misc]


class TestUTV2Schema3AnalysisCLIModelsConfigDefaults:
    """UT-v2-schema-3: AnalysisCLIModelsConfig defaults all fields to empty string."""

    def test_all_empty_by_default(self) -> None:
        from secondsight.config.schema import AnalysisCLIModelsConfig

        cfg = AnalysisCLIModelsConfig()
        assert cfg.claude_code == ""
        assert cfg.codex == ""
        assert cfg.opencode == ""

    def test_frozen(self) -> None:
        from secondsight.config.schema import AnalysisCLIModelsConfig

        cfg = AnalysisCLIModelsConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.claude_code = "claude-opus"  # type: ignore[misc]


class TestUTV2Schema4AnalysisCLIConfigDefaults:
    """UT-v2-schema-4: AnalysisCLIConfig defaults."""

    def test_default_agent_is_auto(self) -> None:
        from secondsight.config.schema import AnalysisCLIConfig

        cfg = AnalysisCLIConfig()
        assert cfg.default_agent == "auto"

    def test_models_is_cli_models_config(self) -> None:
        from secondsight.config.schema import AnalysisCLIConfig, AnalysisCLIModelsConfig

        cfg = AnalysisCLIConfig()
        assert isinstance(cfg.models, AnalysisCLIModelsConfig)

    def test_frozen(self) -> None:
        from secondsight.config.schema import AnalysisCLIConfig

        cfg = AnalysisCLIConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.default_agent = "claude_code"  # type: ignore[misc]


class TestUTV2Schema5AnalysisSDKConfigDefaults:
    """UT-v2-schema-5: AnalysisSDKConfig defaults match config.example.toml.

    Contract revision 2026-05-15: both defaults are empty strings — SDK mode
    refuses to ship a provider-bound default (see TestUTV2Schema10... class
    docstring for the full rationale). precheck enforces explicit operator
    choice before sdk mode can start.
    """

    def test_primary_model_default(self) -> None:
        from secondsight.config.schema import AnalysisSDKConfig

        cfg = AnalysisSDKConfig()
        assert cfg.primary_model == ""

    def test_fallback_model_default(self) -> None:
        from secondsight.config.schema import AnalysisSDKConfig

        cfg = AnalysisSDKConfig()
        assert cfg.fallback_model == ""

    def test_frozen(self) -> None:
        from secondsight.config.schema import AnalysisSDKConfig

        cfg = AnalysisSDKConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.primary_model = "other"  # type: ignore[misc]


class TestUTV2Schema6AnalysisConfigDefaults:
    """UT-v2-schema-6: AnalysisConfig (new aggregate) defaults."""

    def test_timeout_seconds_default(self) -> None:
        from secondsight.config.schema import AnalysisConfig

        cfg = AnalysisConfig()
        assert cfg.timeout_seconds == 300

    def test_cli_is_cli_config(self) -> None:
        from secondsight.config.schema import AnalysisCLIConfig, AnalysisConfig

        cfg = AnalysisConfig()
        assert isinstance(cfg.cli, AnalysisCLIConfig)

    def test_sdk_is_sdk_config(self) -> None:
        from secondsight.config.schema import AnalysisConfig, AnalysisSDKConfig

        cfg = AnalysisConfig()
        assert isinstance(cfg.sdk, AnalysisSDKConfig)

    def test_frozen(self) -> None:
        from secondsight.config.schema import AnalysisConfig

        cfg = AnalysisConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.timeout_seconds = 600  # type: ignore[misc]


class TestUTV2Schema7ProvidersConfigDefaults:
    """UT-v2-schema-7: ProvidersConfig defaults."""

    def test_providers_config_importable(self) -> None:
        from secondsight.config.schema import ProvidersConfig  # noqa: F401

    def test_providers_has_anthropic(self) -> None:
        from secondsight.config.schema import ProvidersConfig, ProviderAnthropicConfig

        cfg = ProvidersConfig()
        assert isinstance(cfg.anthropic, ProviderAnthropicConfig)

    def test_providers_has_openai(self) -> None:
        from secondsight.config.schema import ProvidersConfig, ProviderOpenAIConfig

        cfg = ProvidersConfig()
        assert isinstance(cfg.openai, ProviderOpenAIConfig)

    def test_providers_has_custom(self) -> None:
        from secondsight.config.schema import ProvidersConfig, ProviderCustomConfig

        cfg = ProvidersConfig()
        assert isinstance(cfg.custom, ProviderCustomConfig)

    def test_frozen(self) -> None:
        from secondsight.config.schema import ProvidersConfig

        cfg = ProvidersConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.anthropic = None  # type: ignore[misc]


class TestUTV2Schema8SecondSightConfigNewFields:
    """UT-v2-schema-8: SecondSightConfig has the new fields general, providers, analysis."""

    def test_has_general(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import SecondSightConfig

        assert "general" in {f.name for f in fields(SecondSightConfig)}

    def test_has_providers(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import SecondSightConfig

        assert "providers" in {f.name for f in fields(SecondSightConfig)}

    def test_analysis_field_is_new_analysis_config_type(self) -> None:
        """analysis field in SecondSightConfig must be the new AnalysisConfig, not GlobalAnalysisConfig."""
        from secondsight.config.schema import AnalysisConfig, SecondSightConfig

        # analysis field type should reference the NEW AnalysisConfig
        f = SecondSightConfig.__dataclass_fields__["analysis"]
        assert "AnalysisConfig" in str(f.type) or f.type is AnalysisConfig


class TestUTV2Schema9AllDataclassesFrozen:
    """UT-v2-schema-9: All new dataclasses are frozen (immutable after construction)."""

    def test_provider_openai_config_frozen(self) -> None:
        from secondsight.config.schema import ProviderOpenAIConfig

        cfg = ProviderOpenAIConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.OPENAI_API_KEY = "test"  # type: ignore[misc]

    def test_provider_custom_config_frozen(self) -> None:
        from secondsight.config.schema import ProviderCustomConfig

        cfg = ProviderCustomConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.API_KEY = "test"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit tests for IMPORTANT FIX 4 — SDK model defaults as named constants
# ---------------------------------------------------------------------------


class TestUTV2Schema10SDKModelConstants:
    """UT-v2-schema-10: SDK model defaults are named constants (single source of truth).

    DRY violation from original implementation:
    - AnalysisSDKConfig.primary_model default: hardcoded model id
    - loader.py _build_analysis_config fallback: hardcoded model id
    - Same for fallback_model and timeout_seconds

    After fix: constants BUILTIN_SDK_PRIMARY_MODEL, BUILTIN_SDK_FALLBACK_MODEL,
    BUILTIN_ANALYSIS_TIMEOUT_SECONDS exist in schema.py and are used by both
    schema defaults and loader fallbacks.

    Death test: before fix, these constants do not exist → ImportError.

    Contract reversal 2026-05-15 — both SDK model constants MUST be empty strings:
    SDK mode calls provider APIs directly. SecondSight cannot predict which
    provider/model the operator has access to, so any non-empty default would
    silently bind every SDK user to one specific provider. The fix is to ship
    empty defaults and let precheck (config/precheck.py:279) force explicit
    operator choice. If a future PR repopulates the constants with a real model
    id, the assertions below break — that's the point.
    BUILTIN_ANALYSIS_TIMEOUT_SECONDS stays non-zero (timeout has a sane default;
    provider/model does not).
    """

    def test_builtin_sdk_primary_model_constant_exists(self) -> None:
        from secondsight.config.schema import BUILTIN_SDK_PRIMARY_MODEL  # noqa: F401

    def test_builtin_sdk_fallback_model_constant_exists(self) -> None:
        from secondsight.config.schema import BUILTIN_SDK_FALLBACK_MODEL  # noqa: F401

    def test_builtin_analysis_timeout_seconds_constant_exists(self) -> None:
        from secondsight.config.schema import BUILTIN_ANALYSIS_TIMEOUT_SECONDS  # noqa: F401

    def test_sdk_primary_model_constant_is_empty(self) -> None:
        """Contract: BUILTIN_SDK_PRIMARY_MODEL MUST be empty so precheck forces
        operator choice. A non-empty default silently binds SDK users to a single
        provider/model — wrong by construction (see class docstring).
        """
        from secondsight.config.schema import BUILTIN_SDK_PRIMARY_MODEL

        assert isinstance(BUILTIN_SDK_PRIMARY_MODEL, str)
        assert BUILTIN_SDK_PRIMARY_MODEL == "", (
            f"BUILTIN_SDK_PRIMARY_MODEL must be '' (empty) so SDK mode requires "
            f"explicit operator choice. Got {BUILTIN_SDK_PRIMARY_MODEL!r}. "
            "If you intentionally added a default, also update precheck and "
            "the rationale comments in schema.py and config.example.toml."
        )

    def test_sdk_fallback_model_constant_is_empty(self) -> None:
        """Contract: BUILTIN_SDK_FALLBACK_MODEL MUST be empty for the same
        reason as primary_model — and additionally because "no fallback" is a
        valid sdk-mode posture (per Decision E3 collapse to single fallback).
        """
        from secondsight.config.schema import BUILTIN_SDK_FALLBACK_MODEL

        assert isinstance(BUILTIN_SDK_FALLBACK_MODEL, str)
        assert BUILTIN_SDK_FALLBACK_MODEL == "", (
            f"BUILTIN_SDK_FALLBACK_MODEL must be '' (empty). Got {BUILTIN_SDK_FALLBACK_MODEL!r}."
        )

    def test_analysis_timeout_constant_value(self) -> None:
        from secondsight.config.schema import BUILTIN_ANALYSIS_TIMEOUT_SECONDS

        assert isinstance(BUILTIN_ANALYSIS_TIMEOUT_SECONDS, int)
        assert BUILTIN_ANALYSIS_TIMEOUT_SECONDS > 0

    def test_analysis_sdk_config_default_matches_constant(self) -> None:
        """AnalysisSDKConfig default primary_model must equal the constant."""
        from secondsight.config.schema import BUILTIN_SDK_PRIMARY_MODEL, AnalysisSDKConfig

        cfg = AnalysisSDKConfig()
        assert cfg.primary_model == BUILTIN_SDK_PRIMARY_MODEL, (
            f"AnalysisSDKConfig.primary_model default {cfg.primary_model!r} must equal "
            f"BUILTIN_SDK_PRIMARY_MODEL {BUILTIN_SDK_PRIMARY_MODEL!r}. "
            "Two hardcoded strings can silently diverge when one is updated."
        )

    def test_analysis_sdk_config_fallback_matches_constant(self) -> None:
        """AnalysisSDKConfig default fallback_model must equal the constant."""
        from secondsight.config.schema import BUILTIN_SDK_FALLBACK_MODEL, AnalysisSDKConfig

        cfg = AnalysisSDKConfig()
        assert cfg.fallback_model == BUILTIN_SDK_FALLBACK_MODEL, (
            f"AnalysisSDKConfig.fallback_model default {cfg.fallback_model!r} must equal "
            f"BUILTIN_SDK_FALLBACK_MODEL {BUILTIN_SDK_FALLBACK_MODEL!r}."
        )

    def test_analysis_config_timeout_matches_constant(self) -> None:
        """AnalysisConfig default timeout_seconds must equal the constant."""
        from secondsight.config.schema import BUILTIN_ANALYSIS_TIMEOUT_SECONDS, AnalysisConfig

        cfg = AnalysisConfig()
        assert cfg.timeout_seconds == BUILTIN_ANALYSIS_TIMEOUT_SECONDS, (
            f"AnalysisConfig.timeout_seconds default {cfg.timeout_seconds!r} must equal "
            f"BUILTIN_ANALYSIS_TIMEOUT_SECONDS {BUILTIN_ANALYSIS_TIMEOUT_SECONDS!r}."
        )

    def test_constants_exported_in_all(self) -> None:
        """Named constants must be in __all__ for discoverability."""
        import secondsight.config.schema as schema_mod

        all_exports = schema_mod.__all__
        assert "BUILTIN_SDK_PRIMARY_MODEL" in all_exports, (
            "BUILTIN_SDK_PRIMARY_MODEL must be in __all__ for external discoverability."
        )
        assert "BUILTIN_SDK_FALLBACK_MODEL" in all_exports, (
            "BUILTIN_SDK_FALLBACK_MODEL must be in __all__."
        )
        assert "BUILTIN_ANALYSIS_TIMEOUT_SECONDS" in all_exports, (
            "BUILTIN_ANALYSIS_TIMEOUT_SECONDS must be in __all__."
        )
