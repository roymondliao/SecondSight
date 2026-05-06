"""Death tests for RetentionConfig — task-A1 of GUR-147.

Samsara discipline: death tests first.

Death cases (from changes/2026-05-06_gur-107_phase3a-retention-observation-api/2-plan.md §5):
    DC-6:  Malformed per-project TOML raises RetentionConfigError
           (rather than silently falling back to global). Otherwise an
           operator who typo'd `raw_traces_ttl_days = "ninety"` would
           see "cleanup ran with 90d" and never know the override was
           ignored.
    DC-6b: No config files present (neither global nor per-project)
           returns the built-in default (90, source=builtin_default)
           and does NOT raise. Otherwise every fresh install bricks on
           first cleanup.

Verified facts grounding these tests:
    - No existing TOML config infrastructure (verified C1 in
      plan-verification.md). RetentionConfig defines the file format.
    - Python 3.14 is pinned (.python-version, pyproject.toml). stdlib
      tomllib is available.
    - Built-in default is 90 days per SD §3.10.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secondsight.storage.retention import (
    BUILTIN_DEFAULT_TTL_DAYS,
    RetentionConfig,
    RetentionConfigError,
)


# ----------------------------------------------------------------------
# DC-6b — no config files present → built-in default, never raise
# ----------------------------------------------------------------------


class TestDC6bFreshInstallNoConfig:
    """A fresh install has no config files. RetentionConfig must
    return the built-in default, not raise."""

    def test_no_global_no_per_project_returns_builtin_default(self, tmp_path: Path) -> None:
        # Arrange: tmp_path mimics ~/.secondsight with NO config.toml,
        # and projects/proj-alpha/ also has no config.toml.
        home = tmp_path
        (home / "projects" / "proj-alpha").mkdir(parents=True)

        # Act
        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")

        # Assert — must use built-in default and label its source.
        assert cfg.raw_traces_ttl_days == BUILTIN_DEFAULT_TTL_DAYS == 90
        assert cfg.source == "builtin_default"

    def test_no_global_no_per_project_does_not_raise(self, tmp_path: Path) -> None:
        """Explicit DC-6b: must NOT raise on missing files."""
        home = tmp_path
        # No `projects/{pid}/` directory either — even more naked install.
        # Should still return defaults rather than raise FileNotFoundError.
        cfg = RetentionConfig.load(home=home, project_id="never-seen-before")
        assert cfg.raw_traces_ttl_days == BUILTIN_DEFAULT_TTL_DAYS

    def test_only_per_project_dir_missing_uses_global(self, tmp_path: Path) -> None:
        # Global present, per-project dir does not exist.
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 60\n")
        cfg = RetentionConfig.load(home=home, project_id="never-existed")
        assert cfg.raw_traces_ttl_days == 60
        assert cfg.source == "global_config"


# ----------------------------------------------------------------------
# DC-6 — malformed TOML must raise, not silently fall back
# ----------------------------------------------------------------------


class TestDC6MalformedTomlRaises:
    """Malformed configs must raise RetentionConfigError so the
    operator notices their typo. Silent fallback to global hides the
    bug."""

    def test_malformed_per_project_toml_raises(self, tmp_path: Path) -> None:
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        # Not even valid TOML — missing closing bracket.
        (proj / "config.toml").write_text("[retention\nraw_traces_ttl_days = 30\n")

        with pytest.raises(RetentionConfigError) as exc_info:
            RetentionConfig.load(home=home, project_id="proj-alpha")

        # Error message must surface the project so the operator can
        # locate the offending file. (Silent-failure case 1.)
        assert "proj-alpha" in str(exc_info.value)

    def test_malformed_global_toml_raises(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("not = valid = toml = at = all")

        with pytest.raises(RetentionConfigError):
            RetentionConfig.load(home=home, project_id="proj-alpha")

    def test_per_project_wrong_type_raises(self, tmp_path: Path) -> None:
        """A type error (string instead of int) is also malformed for
        our purposes — silently coercing would hide intent."""
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text('[retention]\nraw_traces_ttl_days = "ninety"\n')

        with pytest.raises(RetentionConfigError) as exc_info:
            RetentionConfig.load(home=home, project_id="proj-alpha")
        assert "raw_traces_ttl_days" in str(exc_info.value)

    def test_per_project_negative_value_raises(self, tmp_path: Path) -> None:
        """Negative TTL would never expire anything OR expire everything
        depending on interpretation — either way operator clearly
        meant something different."""
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\nraw_traces_ttl_days = -5\n")

        with pytest.raises(RetentionConfigError):
            RetentionConfig.load(home=home, project_id="proj-alpha")


# ----------------------------------------------------------------------
# Precedence — per-project overrides global; global overrides default.
# (D4: every cleanup logs the resolved TTL with its source.)
# ----------------------------------------------------------------------


class TestPrecedenceAndSourceAttribution:
    """Each TTL resolution has a provenance: per_project_config,
    global_config, or builtin_default. Source attribution is
    load-bearing — it shows up in cleanup logs (D4)."""

    def test_per_project_overrides_global(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 60\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 180\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 180
        assert cfg.source == "per_project_config"

    def test_global_used_when_per_project_omits_key(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 60\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        # per-project file present but does not override the key
        (proj / "config.toml").write_text("[retention]\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 60
        assert cfg.source == "global_config"

    def test_global_used_when_per_project_lacks_retention_section(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 45\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        # per-project has unrelated content
        (proj / "config.toml").write_text("[some_other_section]\nfoo = 'bar'\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 45
        assert cfg.source == "global_config"

    def test_builtin_used_when_neither_overrides(self, tmp_path: Path) -> None:
        home = tmp_path
        # Both files present but neither has a retention key.
        (home / "config.toml").write_text("[other]\nfoo = 1\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[other]\nfoo = 2\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 90
        assert cfg.source == "builtin_default"
