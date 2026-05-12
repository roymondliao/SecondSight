"""Death tests for RetentionConfig — task-A1 of GUR-147 + task-B1 of GUR-149.

Samsara discipline: death tests first.

Death cases (GUR-147):
    DC-6:  Malformed per-project TOML raises RetentionConfigError
           (rather than silently falling back to global). Otherwise an
           operator who typo'd `raw_traces_ttl_days = "ninety"` would
           see "cleanup ran with 90d" and never know the override was
           ignored.
    DC-6b: No config files present (neither global nor per-project)
           returns the built-in default (90, raw_traces_source=builtin_default)
           and does NOT raise. Otherwise every fresh install bricks on
           first cleanup.

Death cases (GUR-149 task-B1):
    DC-B1: TTL config typo silently uses default. An operator who writes
           `analysis_ttl_day = 30` (missing `s`) must NOT silently get
           30-day enforcement — the loader sees no recognized key and
           falls through to builtin (365). Detection: source attribution
           on the resolved value must surface `builtin_default` so a
           cleanup log line reveals the mismatch to the operator.

Verified facts grounding these tests:
    - No existing TOML config infrastructure (verified C1 in
      plan-verification.md). RetentionConfig defines the file format.
    - Python 3.14 is pinned (.python-version, pyproject.toml). stdlib
      tomllib is available.
    - Built-in default is 90 days for raw_traces, 365 for analysis
      per SD §3.10.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secondsight.storage.retention import (
    BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS,
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
        assert cfg.raw_traces_source == "builtin_default"

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
        assert cfg.raw_traces_source == "global_config"


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
        assert cfg.raw_traces_source == "per_project_config"

    def test_global_used_when_per_project_omits_key(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 60\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        # per-project file present but does not override the key
        (proj / "config.toml").write_text("[retention]\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 60
        assert cfg.raw_traces_source == "global_config"

    def test_global_used_when_per_project_lacks_retention_section(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 45\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        # per-project has unrelated content
        (proj / "config.toml").write_text("[some_other_section]\nfoo = 'bar'\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 45
        assert cfg.raw_traces_source == "global_config"

    def test_builtin_used_when_neither_overrides(self, tmp_path: Path) -> None:
        home = tmp_path
        # Both files present but neither has a retention key.
        (home / "config.toml").write_text("[other]\nfoo = 1\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[other]\nfoo = 2\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.raw_traces_ttl_days == 90
        assert cfg.raw_traces_source == "builtin_default"


# ----------------------------------------------------------------------
# GUR-149 task-B1 — analysis_ttl_days resolution
# ----------------------------------------------------------------------


class TestDCB1AnalysisTtlTypoFallsThroughToDefault:
    """DC-B1: An operator who writes `analysis_ttl_day = 30` (missing `s`)
    silently gets the 365-day builtin. The only signal is the resolved
    source attribution (`builtin_default`) — without that, the operator
    has no way to know their override was ignored.

    These tests pin the *detection* contract: the resolved config must
    carry source attribution that distinguishes "operator chose 365" from
    "loader fell through to builtin because the key was misspelled".
    """

    def test_per_project_typo_in_key_falls_through_to_builtin(self, tmp_path: Path) -> None:
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        # Typo: `analysis_ttl_day` (missing `s`).
        (proj / "config.toml").write_text("[retention]\nanalysis_ttl_day = 30\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")

        assert cfg.analysis_ttl_days == BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS == 365
        assert cfg.analysis_ttl_source == "builtin_default"

    def test_global_typo_in_key_falls_through_to_builtin(self, tmp_path: Path) -> None:
        home = tmp_path
        # Typo at global scope.
        (home / "config.toml").write_text("[retention]\nanalisys_ttl_days = 30\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")

        assert cfg.analysis_ttl_days == 365
        assert cfg.analysis_ttl_source == "builtin_default"


class TestAnalysisTtlPrecedence:
    """analysis_ttl_days resolves through the same per-project →
    global → builtin chain as raw_traces_ttl_days, with independent
    source attribution."""

    def test_per_project_overrides_global_for_analysis_ttl(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nanalysis_ttl_days = 60\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\nanalysis_ttl_days = 30\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.analysis_ttl_days == 30
        assert cfg.analysis_ttl_source == "per_project_config"

    def test_global_used_when_per_project_omits_analysis_ttl(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nanalysis_ttl_days = 90\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")
        assert cfg.analysis_ttl_days == 90
        assert cfg.analysis_ttl_source == "global_config"

    def test_builtin_used_when_neither_overrides_analysis_ttl(self, tmp_path: Path) -> None:
        home = tmp_path
        cfg = RetentionConfig.load(home=home, project_id="never-existed")
        assert cfg.analysis_ttl_days == 365
        assert cfg.analysis_ttl_source == "builtin_default"


class TestAnalysisTtlValidation:
    """Same validation rules as raw_traces_ttl_days: bool / non-int /
    non-positive must raise RetentionConfigError. Reuses _validate_ttl."""

    def test_per_project_analysis_ttl_string_raises(self, tmp_path: Path) -> None:
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text('[retention]\nanalysis_ttl_days = "thirty"\n')

        with pytest.raises(RetentionConfigError) as exc_info:
            RetentionConfig.load(home=home, project_id="proj-alpha")
        assert "analysis_ttl_days" in str(exc_info.value)

    def test_per_project_analysis_ttl_negative_raises(self, tmp_path: Path) -> None:
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\nanalysis_ttl_days = -5\n")

        with pytest.raises(RetentionConfigError):
            RetentionConfig.load(home=home, project_id="proj-alpha")

    def test_per_project_analysis_ttl_bool_raises(self, tmp_path: Path) -> None:
        """A bool is technically an int in Python; reject explicitly so
        `analysis_ttl_days = true` is treated as a typo, not 1 day."""
        home = tmp_path
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\nanalysis_ttl_days = true\n")

        with pytest.raises(RetentionConfigError):
            RetentionConfig.load(home=home, project_id="proj-alpha")


class TestIndependentSourceAttribution:
    """raw_traces and analysis TTLs resolve independently. A config that
    sets one per-project and the other globally must carry distinct
    source attributions on each field."""

    def test_per_project_raw_only_global_analysis(self, tmp_path: Path) -> None:
        home = tmp_path
        (home / "config.toml").write_text("[retention]\nanalysis_ttl_days = 60\n")
        proj = home / "projects" / "proj-alpha"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text("[retention]\nraw_traces_ttl_days = 30\n")

        cfg = RetentionConfig.load(home=home, project_id="proj-alpha")

        assert cfg.raw_traces_ttl_days == 30
        assert cfg.raw_traces_source == "per_project_config"
        assert cfg.analysis_ttl_days == 60
        assert cfg.analysis_ttl_source == "global_config"
