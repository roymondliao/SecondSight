"""Tests for AnalysisConfig TOML loader (GUR-103 task-1, P2-11).

Modeled on tests/storage/test_retention_config.py (GUR-147 pattern).

Death cases:
- DC-C1: Malformed TOML raises AnalysisConfigError (not silently using defaults).
- DC-C2: Wrong-type value raises AnalysisConfigError (not coercing silently).
- DC-C3: Negative/zero size_cap_kb raises AnalysisConfigError.
- DC-C4: denylist must be ADDITIVE — user cannot supply a value that disables
          the built-in denylist entries.

Happy-path:
- HP-C1: No config file → all defaults.
- HP-C2: [analysis.read_project_file] enabled = false → disable flag respected.
- HP-C3: denylist additions in config are merged with built-in.
- HP-C4: size_cap_kb override is respected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secondsight.analysis.config import AnalysisConfig, AnalysisConfigError

BUILTIN_SIZE_CAP_KB = 256


class TestDCMalformedToml:
    """Malformed TOML must raise AnalysisConfigError, never silently fallback."""

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis\nenabled = true\n")  # bad TOML

        with pytest.raises(AnalysisConfigError):
            AnalysisConfig.load(config_path=config_file)

    def test_wrong_type_size_cap_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('[analysis.read_project_file]\nsize_cap_kb = "lots"\n')

        with pytest.raises(AnalysisConfigError):
            AnalysisConfig.load(config_path=config_file)

    def test_negative_size_cap_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\nsize_cap_kb = -1\n")

        with pytest.raises(AnalysisConfigError):
            AnalysisConfig.load(config_path=config_file)

    def test_zero_size_cap_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\nsize_cap_kb = 0\n")

        with pytest.raises(AnalysisConfigError):
            AnalysisConfig.load(config_path=config_file)


class TestHPNoConfigReturnsDefaults:
    """No config file → all defaults; never raise."""

    def test_missing_config_returns_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "does_not_exist.toml"

        config = AnalysisConfig.load(config_path=config_file)

        assert config.read_project_file_enabled is True
        assert config.size_cap_kb == BUILTIN_SIZE_CAP_KB
        assert config.extra_denylist == []

    def test_missing_config_never_raises(self, tmp_path: Path) -> None:
        config = AnalysisConfig.load(config_path=tmp_path / "nope.toml")
        assert config is not None


class TestHPDisableFlagRespected:
    """[analysis.read_project_file] enabled = false → read_project_file_enabled is False."""

    def test_disable_flag_respected(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\nenabled = false\n")

        config = AnalysisConfig.load(config_path=config_file)

        assert config.read_project_file_enabled is False

    def test_explicit_enable_true_respected(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\nenabled = true\n")

        config = AnalysisConfig.load(config_path=config_file)

        assert config.read_project_file_enabled is True


class TestHPDenylistMerge:
    """User denylist additions are merged with built-in (additive only)."""

    def test_denylist_additions_included(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[analysis.read_project_file]\ndenylist = ["*.log", "passwords.txt"]\n'
        )

        config = AnalysisConfig.load(config_path=config_file)

        assert "*.log" in config.extra_denylist
        assert "passwords.txt" in config.extra_denylist

    def test_empty_denylist_section_no_extras(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\ndenylist = []\n")

        config = AnalysisConfig.load(config_path=config_file)

        assert config.extra_denylist == []


class TestHPSizeCapOverride:
    """size_cap_kb can be overridden via config."""

    def test_size_cap_override(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\nsize_cap_kb = 512\n")

        config = AnalysisConfig.load(config_path=config_file)

        assert config.size_cap_kb == 512

    def test_size_cap_1_is_valid_minimum(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[analysis.read_project_file]\nsize_cap_kb = 1\n")

        config = AnalysisConfig.load(config_path=config_file)

        assert config.size_cap_kb == 1
