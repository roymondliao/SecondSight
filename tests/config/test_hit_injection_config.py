"""Death + unit tests for [feedback].hit_injection_enabled config toggle (task-3).

Death tests are placed before unit tests per samsara framework ordering.

DT-4: Config key missing → silent default to bool True.
  test_dt_config_resolves_missing_hit_injection_enabled_to_true_bool
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secondsight.config.loader import load_global_config


# ---------------------------------------------------------------------------
# Death tests
# ---------------------------------------------------------------------------


def test_dt_config_resolves_missing_hit_injection_enabled_to_true_bool(
    tmp_path: Path,
) -> None:
    """DT-4: missing key must silently default to Python bool True.

    If the default were a truthy string ("true", "1") or any non-bool,
    downstream type checks would pass but `is True` would fail.  The hook
    reads this as a bool; a string default would silently break the
    enabled/disabled gate.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()
    # Write a [feedback] section WITHOUT hit_injection_enabled.
    # The loader must produce exactly True (identity), not a truthy surrogate.
    config_toml = home / "config.toml"
    config_toml.write_text(
        "[feedback]\nconvention_injection_budget = 2000\nconvention_top_n = 15\n",
        encoding="utf-8",
    )

    cfg = load_global_config(home)
    value = cfg.feedback.hit_injection_enabled

    # Use `is True` (identity), not `== True` (equality), to catch truthy strings.
    assert value is True, (
        f"hit_injection_enabled defaulted to {value!r} (type {type(value).__name__}), "
        "expected exactly Python bool True"
    )


def test_dt_config_resolves_entirely_missing_feedback_section_to_true(
    tmp_path: Path,
) -> None:
    """DT-4 extension: no [feedback] section at all → hit_injection_enabled is True.

    Guards against the loader returning None or raising when the section is absent.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()
    # Write config.toml with no [feedback] section at all.
    config_toml = home / "config.toml"
    config_toml.write_text('[general]\nmode = "cli"\n', encoding="utf-8")

    cfg = load_global_config(home)
    value = cfg.feedback.hit_injection_enabled

    assert value is True, (
        f"hit_injection_enabled defaulted to {value!r} (type {type(value).__name__}), "
        "expected exactly Python bool True"
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_hit_injection_enabled_explicit_true(tmp_path: Path) -> None:
    """Explicit hit_injection_enabled = true in TOML resolves to Python bool True."""
    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = true\n",
        encoding="utf-8",
    )

    cfg = load_global_config(home)
    assert cfg.feedback.hit_injection_enabled is True


def test_hit_injection_enabled_explicit_false(tmp_path: Path) -> None:
    """Explicit hit_injection_enabled = false in TOML resolves to Python bool False."""
    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = false\n",
        encoding="utf-8",
    )

    cfg = load_global_config(home)
    assert cfg.feedback.hit_injection_enabled is False


def test_hit_injection_enabled_absent_config_file(tmp_path: Path) -> None:
    """No config.toml at all → hit_injection_enabled defaults to True."""
    home = tmp_path / ".secondsight"
    home.mkdir()
    # No config.toml written.

    cfg = load_global_config(home)
    assert cfg.feedback.hit_injection_enabled is True


def test_dt_project_scope_override_wins_over_global(tmp_path: Path) -> None:
    """I-2 (death test): project-scope hit_injection_enabled = false must win over global = true.

    Silent failure path: if the project→global→builtin priority chain is broken,
    a per-project override of false is silently ignored, and injection keeps
    running even though the operator explicitly disabled it for that project.

    This tests the _resolve_feedback_typed_field priority chain in loader.py,
    specifically that project_section takes precedence over global_section.
    """
    from secondsight.config.loader import load_project_config

    home = tmp_path / ".secondsight"
    home.mkdir()

    # Global: injection enabled.
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = true\n",
        encoding="utf-8",
    )

    # Project: injection DISABLED — must win over global.
    project_dir = home / "projects" / "my-project"
    project_dir.mkdir(parents=True)
    (project_dir / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = false\n",
        encoding="utf-8",
    )

    cfg = load_project_config(home, "my-project")
    value = cfg.feedback.hit_injection_enabled

    assert value is False, (
        f"Project-scope override (false) must win over global (true); "
        f"got {value!r} (type {type(value).__name__})"
    )


def test_hit_injection_enabled_invalid_value_raises(tmp_path: Path) -> None:
    """Non-bool hit_injection_enabled value in TOML must raise SecondSightConfigError."""
    from secondsight.config.schema import SecondSightConfigError

    home = tmp_path / ".secondsight"
    home.mkdir()
    (home / "config.toml").write_text(
        "[feedback]\nhit_injection_enabled = 1\n",  # integer, not bool
        encoding="utf-8",
    )

    with pytest.raises(SecondSightConfigError, match="hit_injection_enabled"):
        load_global_config(home)
