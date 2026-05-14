"""Tests for config/schema.py — death tests first.

DT-schema-1: ProjectAnalysisConfig(model="").model == "" (schema does NOT reject empty string;
    empty string semantics are documented in schema docstring but rejection is the loader's job).
    This test ALSO verifies that the empty string is NOT treated as a valid override at the
    schema level — schema must preserve it transparently so the loader can detect "not set".

DT-schema-2: SecondSightConfig and SecondSightConfigError must exist in config.schema.
    These are new additions that do not exist anywhere yet — death tests red until schema.py
    is implemented.

DT-schema-3: RetentionConfig imported from config.schema must be identical object to the
    one in storage.retention (same class, not a copy). After re-export, isinstance checks
    across modules must still pass.

DT-schema-4: ModelsConfig, GlobalAnalysisConfig, ProjectAnalysisConfig all importable
    from config.schema (not just from analysis.config).

Unit tests:
- UT-schema-1: ProjectAnalysisConfig defaults (model="").
- UT-schema-2: GlobalAnalysisConfig defaults (default_agent="claude_code").
- UT-schema-3: FallbackModelsConfig defaults.
- UT-schema-4: SecondSightConfig composes RetentionConfig + GlobalAnalysisConfig +
    ProjectAnalysisConfig.
- UT-schema-5: SecondSightConfigError is an Exception subclass.
- UT-schema-6: backward-compat — all existing classes still importable from analysis.config.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Death tests — these MUST fail (red) before schema.py is implemented
# ---------------------------------------------------------------------------


class TestDTSchema1EmptyStringPreserved:
    """DT-schema-1: empty string model must be preserved as-is by schema.

    Schema does NOT reject empty string. The semantics (empty = not set) are
    documented in ProjectAnalysisConfig docstring. Rejection is the loader's job.
    If schema accidentally coerces "" to None or raises, the loader cannot
    distinguish "user set empty" from "loader skipped the field entirely".
    """

    def test_empty_model_is_preserved(self) -> None:
        from secondsight.config.schema import ProjectAnalysisConfig

        cfg = ProjectAnalysisConfig(model="")
        assert cfg.model == "", (
            "Schema must preserve model='' as empty string, not coerce to None or raise. "
            "Rejection of empty model happens at loader level (task-2), not schema level."
        )

    def test_empty_model_is_not_truthy(self) -> None:
        """Verify the loader can detect 'not set' via `if not cfg.model`."""
        from secondsight.config.schema import ProjectAnalysisConfig

        cfg = ProjectAnalysisConfig(model="")
        assert not cfg.model, (
            "Empty string model must be falsy so loader can do `if not cfg.model` "
            "to detect 'not set'. If this fails, silent override promotion occurs."
        )

    def test_nonempty_model_is_preserved(self) -> None:
        """Non-empty model must also be preserved unmodified."""
        from secondsight.config.schema import ProjectAnalysisConfig

        cfg = ProjectAnalysisConfig(model="claude-opus-4-5")
        assert cfg.model == "claude-opus-4-5"


class TestDTSchema2NewClassesExist:
    """DT-schema-2: SecondSightConfig and SecondSightConfigError must exist in config.schema."""

    def test_secondsight_config_importable(self) -> None:
        from secondsight.config.schema import SecondSightConfig  # noqa: F401

    def test_secondsight_config_error_importable(self) -> None:
        from secondsight.config.schema import SecondSightConfigError  # noqa: F401

    def test_secondsight_config_error_is_exception(self) -> None:
        from secondsight.config.schema import SecondSightConfigError

        assert issubclass(SecondSightConfigError, Exception)


class TestDTSchema3RetentionConfigIdentity:
    """DT-schema-3: RetentionConfig from config.schema must be the SAME class as from
    storage.retention — not a copy. After re-export, isinstance checks must pass across
    module boundaries.
    """

    def test_retention_config_same_class(self) -> None:
        from secondsight.config.schema import RetentionConfig as SchemaRetentionConfig
        from secondsight.storage.retention import RetentionConfig as StorageRetentionConfig

        assert SchemaRetentionConfig is StorageRetentionConfig, (
            "RetentionConfig imported from config.schema must be the exact same class as "
            "from storage.retention. If these diverge, isinstance() checks across modules "
            "will silently fail — e.g. code that checks isinstance(obj, RetentionConfig) "
            "will return False for objects created with the other class."
        )


class TestDTSchema4ModelConfigsImportable:
    """DT-schema-4: All model-selection config classes must be importable from config.schema."""

    def test_models_config_importable(self) -> None:
        from secondsight.config.schema import ModelsConfig  # noqa: F401

    def test_global_analysis_config_importable(self) -> None:
        from secondsight.config.schema import GlobalAnalysisConfig  # noqa: F401

    def test_project_analysis_config_importable(self) -> None:
        from secondsight.config.schema import ProjectAnalysisConfig  # noqa: F401

    def test_fallback_models_config_importable(self) -> None:
        from secondsight.config.schema import FallbackModelsConfig  # noqa: F401

    def test_analysis_config_in_schema_is_new_aggregate(self) -> None:
        """AnalysisConfig IS importable from config.schema — but it is the NEW aggregate class.

        analysis-mode-toggle task-1 adds AnalysisConfig to config.schema as the
        [analysis] / [analysis.cli] / [analysis.sdk] aggregate. This is a DIFFERENT
        class from secondsight.analysis.config.AnalysisConfig (the per-project TOML reader).

        The two classes must remain distinct (different objects) so there is no class
        identity split for isinstance() checks on the per-project AnalysisConfig.
        """
        from secondsight.config.schema import AnalysisConfig  # noqa: F401
        from secondsight.analysis.config import AnalysisConfig as OldAnalysisConfig

        assert AnalysisConfig is not OldAnalysisConfig, (
            "config.schema.AnalysisConfig (new aggregate) must be a DIFFERENT class "
            "from analysis.config.AnalysisConfig (per-project TOML reader). "
            "Same class would create circular import dependency."
        )


# ---------------------------------------------------------------------------
# Unit tests — verify correct schema behavior after implementation
# ---------------------------------------------------------------------------


class TestUTProjectAnalysisConfigDefaults:
    """UT-schema-1: ProjectAnalysisConfig defaults."""

    def test_default_model_is_empty_string(self) -> None:
        from secondsight.config.schema import ProjectAnalysisConfig

        cfg = ProjectAnalysisConfig()
        assert cfg.model == ""

    def test_frozen(self) -> None:
        from secondsight.config.schema import ProjectAnalysisConfig

        cfg = ProjectAnalysisConfig(model="gpt-4o")
        with pytest.raises((AttributeError, TypeError)):
            cfg.model = "other"  # type: ignore[misc]


class TestUTGlobalAnalysisConfigDefaults:
    """UT-schema-2: GlobalAnalysisConfig defaults."""

    def test_default_agent(self) -> None:
        from secondsight.config.schema import GlobalAnalysisConfig

        cfg = GlobalAnalysisConfig()
        assert cfg.default_agent == "claude_code"

    def test_default_models(self) -> None:
        from secondsight.config.schema import GlobalAnalysisConfig, ModelsConfig

        cfg = GlobalAnalysisConfig()
        assert isinstance(cfg.models, ModelsConfig)


class TestUTFallbackModelsConfigDefaults:
    """UT-schema-3: FallbackModelsConfig defaults."""

    def test_default_fallback_models(self) -> None:
        from secondsight.config.schema import FallbackModelsConfig

        cfg = FallbackModelsConfig()
        assert isinstance(cfg.fallback_models, list)
        assert len(cfg.fallback_models) > 0

    def test_empty_fallback_models_valid(self) -> None:
        """Empty fallback list is valid (strict-mode — no fallback)."""
        from secondsight.config.schema import FallbackModelsConfig

        cfg = FallbackModelsConfig(fallback_models=[])
        assert cfg.fallback_models == []


class TestUTSecondSightConfigComposition:
    """UT-schema-4: SecondSightConfig composes RetentionConfig + GlobalAnalysisConfig +
    ProjectAnalysisConfig.
    """

    def test_secondsight_config_fields_exist(self) -> None:
        from dataclasses import fields

        from secondsight.config.schema import SecondSightConfig

        field_names = {f.name for f in fields(SecondSightConfig)}
        assert "retention" in field_names
        assert "analysis" in field_names
        assert "project_analysis" in field_names

    def test_secondsight_config_field_types(self) -> None:
        from secondsight.config.schema import (
            AnalysisConfig,
            ProjectAnalysisConfig,
            RetentionConfig,
            SecondSightConfig,
        )

        from dataclasses import fields

        field_map = {f.name: f for f in fields(SecondSightConfig)}
        assert field_map["retention"].type is RetentionConfig or "RetentionConfig" in str(
            field_map["retention"].type
        )
        # analysis is now AnalysisConfig (new aggregate), not GlobalAnalysisConfig (old flat)
        assert field_map["analysis"].type is AnalysisConfig or "AnalysisConfig" in str(
            field_map["analysis"].type
        )
        assert field_map["project_analysis"].type is ProjectAnalysisConfig or (
            "ProjectAnalysisConfig" in str(field_map["project_analysis"].type)
        )


class TestUTImportChainIntegrity:
    """Verify the import chain from config.schema through analysis.config to sdk.model_selection
    does not produce circular imports or RuntimeError from _verify_adapter_registry_consistency.
    """

    def test_model_selection_import_succeeds(self) -> None:
        """sdk.model_selection imports successfully after ModelsConfig migration.

        _verify_adapter_registry_consistency() runs at import time and would raise
        RuntimeError if ModelsConfig fields diverge from the adapter registry.
        This test catches any import-time breakage immediately.
        """
        import secondsight.sdk.model_selection as ms  # noqa: F401

        # If import succeeded, _verify_adapter_registry_consistency() passed.
        assert hasattr(ms, "select_model")

    def test_no_circular_import_config_to_storage(self) -> None:
        """config.schema can import RetentionConfig from storage.retention without cycle."""
        # If this import fails with ImportError or RecursionError, circular import exists.
        from secondsight.config.schema import RetentionConfig  # noqa: F401
        from secondsight.storage.retention import RetentionConfig as StorageRC  # noqa: F401

        # Both must succeed — if either fails, the import chain is broken.
        assert RetentionConfig is StorageRC


class TestUTBackwardCompatibility:
    """UT-schema-6: All existing classes must still be importable from analysis.config."""

    def test_analysis_config_still_importable(self) -> None:
        from secondsight.analysis.config import AnalysisConfig  # noqa: F401

    def test_analysis_config_error_still_importable(self) -> None:
        from secondsight.analysis.config import AnalysisConfigError  # noqa: F401

    def test_models_config_still_importable_from_analysis(self) -> None:
        from secondsight.analysis.config import ModelsConfig  # noqa: F401

    def test_global_analysis_config_still_importable_from_analysis(self) -> None:
        from secondsight.analysis.config import GlobalAnalysisConfig  # noqa: F401

    def test_project_analysis_config_still_importable_from_analysis(self) -> None:
        from secondsight.analysis.config import ProjectAnalysisConfig  # noqa: F401

    def test_fallback_models_config_still_importable_from_analysis(self) -> None:
        from secondsight.analysis.config import FallbackModelsConfig  # noqa: F401

    def test_retention_config_still_importable_from_storage(self) -> None:
        from secondsight.storage.retention import RetentionConfig  # noqa: F401

    def test_analysis_config_has_load_method(self) -> None:
        """AnalysisConfig from analysis.config must have .load() classmethod.

        This verifies analysis.config owns the canonical AnalysisConfig with
        TOML loading, and that the schema cleanup did not accidentally remove it.
        If load() is missing, TOML config loading silently falls back to defaults
        for every project — the first person to notice is a user wondering why
        their config file is ignored.
        """
        from secondsight.analysis.config import AnalysisConfig

        assert hasattr(AnalysisConfig, "load"), (
            "AnalysisConfig.load classmethod must exist in analysis.config. "
            "If missing, TOML config is silently ignored for all projects."
        )
        assert callable(AnalysisConfig.load)

    def test_analysis_config_error_is_exception(self) -> None:
        """AnalysisConfigError from analysis.config must be an Exception subclass."""
        from secondsight.analysis.config import AnalysisConfigError

        assert issubclass(AnalysisConfigError, Exception)

    def test_analysis_config_in_schema_is_new_aggregate_not_old_reader(self) -> None:
        """config.schema.AnalysisConfig must be the NEW aggregate (not old per-project reader).

        analysis-mode-toggle task-1 adds a NEW AnalysisConfig to config.schema as the
        [analysis]/[analysis.cli]/[analysis.sdk] aggregate dataclass.
        The OLD AnalysisConfig in analysis.config remains unchanged (per-project TOML reader).
        These MUST be different classes. This test guards against accidental collision.
        """
        import secondsight.config.schema as schema_module
        from secondsight.analysis.config import AnalysisConfig as OldAnalysisConfig

        # config.schema.AnalysisConfig must exist (new aggregate)
        assert hasattr(schema_module, "AnalysisConfig"), (
            "config.schema.AnalysisConfig must exist (added in analysis-mode-toggle task-1)."
        )
        new_cls = schema_module.AnalysisConfig
        # They must be different class objects
        assert new_cls is not OldAnalysisConfig, (
            "config.schema.AnalysisConfig and analysis.config.AnalysisConfig must be "
            "DIFFERENT classes to avoid circular imports and isinstance confusion."
        )
        # The new one must NOT have .load() (that belongs to analysis.config.AnalysisConfig)
        assert not hasattr(new_cls, "load"), (
            "config.schema.AnalysisConfig must NOT have .load() — it is a pure dataclass. "
            "TOML reading belongs in analysis.config.AnalysisConfig."
        )

    def test_analysis_config_error_not_in_schema(self) -> None:
        """AnalysisConfigError must NOT be importable from config.schema."""
        import secondsight.config.schema as schema_module

        assert not hasattr(schema_module, "AnalysisConfigError"), (
            "AnalysisConfigError must NOT be defined in config.schema. "
            "Having it there creates a second class object; except clauses using "
            "the schema version will never catch errors raised by analysis.config."
        )

    def test_analysis_config_class_identity_preserved(self) -> None:
        """Classes from analysis.config must be the same objects as from config.schema."""
        from secondsight.analysis.config import ModelsConfig as AnalysisModelsConfig
        from secondsight.config.schema import ModelsConfig as SchemaModelsConfig

        assert AnalysisModelsConfig is SchemaModelsConfig, (
            "ModelsConfig from analysis.config must be the same class as from config.schema. "
            "_verify_adapter_registry_consistency() imports ModelsConfig from analysis.config; "
            "if they diverge, the consistency check runs against the wrong class."
        )

    def test_global_analysis_config_class_identity(self) -> None:
        """Guard against re-adding GlobalAnalysisConfig definition in analysis.config."""
        from secondsight.analysis.config import GlobalAnalysisConfig as AnalysisGAC
        from secondsight.config.schema import GlobalAnalysisConfig as SchemaGAC

        assert AnalysisGAC is SchemaGAC, (
            "GlobalAnalysisConfig from analysis.config must be the same class as from "
            "config.schema. If they diverge, callers constructing GlobalAnalysisConfig "
            "from one module and passing to code that checks from the other module will "
            "have silent isinstance() failures."
        )

    def test_fallback_models_config_class_identity(self) -> None:
        """Guard against re-adding FallbackModelsConfig definition in analysis.config."""
        from secondsight.analysis.config import FallbackModelsConfig as AnalysisFMC
        from secondsight.config.schema import FallbackModelsConfig as SchemaFMC

        assert AnalysisFMC is SchemaFMC, (
            "FallbackModelsConfig from analysis.config must be the same class as from "
            "config.schema."
        )

    def test_project_analysis_config_class_identity(self) -> None:
        """Guard against re-adding ProjectAnalysisConfig definition in analysis.config."""
        from secondsight.analysis.config import ProjectAnalysisConfig as AnalysisPAC
        from secondsight.config.schema import ProjectAnalysisConfig as SchemaPAC

        assert AnalysisPAC is SchemaPAC, (
            "ProjectAnalysisConfig from analysis.config must be the same class as from "
            "config.schema."
        )
