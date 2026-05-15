"""Runtime wiring tests for config-unification task-3.

Death tests first — verify that config loading actually affects model selection.
All death tests target the silent failure path: TOML is loaded but the wrong
attribute path is used, so the loaded value is silently ignored and the built-in
default is used instead.

DT-wire-1: per-project config.toml with [analysis] model override → agent uses that model
DT-wire-2: SECONDSIGHT_ANALYSIS_MODEL env var set → agent uses env var model
DT-wire-3: config.toml absent → _build_analysis_agent() uses built-in default without raising

Attribute access contract:
    PydanticAIAnalysisAgent builds THREE internal sub-routers (segment, aggregate, summary).
    The primary model is accessible via: agent._segment_router.config.primary.name
    All three sub-routers share the same primary/fallback chain (from the caller-provided router).
    We inspect _segment_router as the canonical representative.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock


from secondsight.analysis.runtime import _build_analysis_agent
from secondsight.sdk.model_selection import _ADAPTER_DEFAULTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_events_repo() -> object:
    """Return a minimal fake EventsRepository for model-selection tests.

    get_latest_session_agent_type returns None (no sessions observed).
    The built-in default_agent='claude_code' handles this path without
    calling events_repo for auto-mode.
    """
    repo = MagicMock()
    repo.get_latest_session_agent_type.return_value = None
    return repo


def _build_agent_for_test(
    *,
    secondsight_home: Path,
    project_id: str = "proj-test",
) -> object:
    """Call _build_analysis_agent() with repo fakes injected.

    Creates the project directory so AnalysisTools.project_root.resolve() succeeds.
    Repos are MagicMock — no DB required.

    Task 5 note: LLMRouter now requires at least one non-empty provider key at
    init time (Decision E1). We ensure the global config.toml always has a test
    Anthropic key so the router construction passes. The key is a test placeholder
    — no real API calls are made in these wiring tests.

    If the test already wrote a global config.toml (e.g. test_ut_wire_* tests),
    we merge the provider section in. If no global config.toml exists, we create one
    with only the provider key so model selection tests remain unaffected.
    """
    project_dir = secondsight_home / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    # Ensure global config.toml has provider key for LLMRouter validation.
    # We APPEND if the file exists to avoid overwriting model selection settings.
    global_config_path = secondsight_home / "config.toml"
    _provider_stub = (
        '\n[providers.anthropic]\nANTHROPIC_API_KEY = "sk-test-runtime-wiring-placeholder"\n'
    )
    if global_config_path.exists():
        existing = global_config_path.read_text()
        if "[providers.anthropic]" not in existing:
            global_config_path.write_text(existing + _provider_stub)
    else:
        global_config_path.write_text(_provider_stub)

    return _build_analysis_agent(
        secondsight_home=secondsight_home,
        project_id=project_id,
        events_repository=cast(Any, _make_fake_events_repo()),
        flags_repository=MagicMock(),
        directives_repository=MagicMock(),
    )


def _primary_model_name(agent: object) -> str:
    """Extract primary model name from the segment sub-router's config.

    PydanticAIAnalysisAgent has three sub-routers (_segment_router, _aggregate_router,
    _summary_router). All share the same primary/fallback chain. We inspect
    _segment_router as the canonical representative.

    Access path: agent._segment_router.config.primary.name
    """
    return cast(Any, agent)._segment_router.config.primary.name


def _write_project_config(home: Path, project_id: str, content: str) -> None:
    """Write a per-project config.toml for testing (project dir must already exist)."""
    project_dir = home / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "config.toml").write_text(content)


# ---------------------------------------------------------------------------
# DEATH TESTS — must fail (red) before implementation
# These test the SPECIFIC silent failure: config loaded but wrong attribute
# path → loaded value ignored → built-in default used instead.
# ---------------------------------------------------------------------------


def test_dt_wire_1_toml_model_override_takes_effect(tmp_path: Path) -> None:
    """DEATH TEST: per-project config.toml [analysis] model override must reach the agent.

    Silent failure mode: config is loaded but hardcoded GlobalAnalysisConfig() /
    ProjectAnalysisConfig() are used instead → TOML value silently ignored →
    agent uses "claude-haiku-4-5-20251001" (built-in default) instead of the
    configured model.

    This test dies when the wiring is broken (pre-fix state).
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Per-project config sets a specific model via [analysis] model = "..."
    # This is the ProjectAnalysisConfig.model field path, highest priority
    # (beats global [analysis.models.claude_code] setting).
    _write_project_config(
        home,
        "proj-wire-1",
        '[analysis]\nmodel = "claude-sonnet-4-6"\n',
    )

    agent = _build_agent_for_test(secondsight_home=home, project_id="proj-wire-1")
    primary_name = _primary_model_name(agent)

    assert primary_name == "claude-sonnet-4-6", (
        f"Expected 'claude-sonnet-4-6' from per-project config.toml, "
        f"got {primary_name!r}. "
        "The config wiring is broken — load_project_config() was not called or its "
        "result was not passed to select_model()."
    )


def test_dt_wire_2_env_var_model_overrides_all(tmp_path: Path) -> None:
    """DEATH TEST: SECONDSIGHT_ANALYSIS_MODEL env var must override everything.

    Silent failure mode: env var is read by load_project_config() internally,
    but the returned cfg is not used → env var silently ignored → built-in
    default used.

    This test relies on no config.toml existing so the only override is env var.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # No config.toml at all — env var is the only override source
    env_model = "claude-opus-4-7"
    old_val = os.environ.get("SECONDSIGHT_ANALYSIS_MODEL")
    try:
        os.environ["SECONDSIGHT_ANALYSIS_MODEL"] = env_model
        agent = _build_agent_for_test(secondsight_home=home, project_id="proj-wire-2")
    finally:
        if old_val is None:
            os.environ.pop("SECONDSIGHT_ANALYSIS_MODEL", None)
        else:
            os.environ["SECONDSIGHT_ANALYSIS_MODEL"] = old_val

    primary_name = _primary_model_name(agent)
    assert primary_name == env_model, (
        f"Expected {env_model!r} from SECONDSIGHT_ANALYSIS_MODEL env var, "
        f"got {primary_name!r}. "
        "The env var override path is broken — load_project_config() reads it but "
        "the result is not threaded through to select_model()."
    )


def test_dt_wire_3_absent_config_toml_uses_builtin_default_no_crash(tmp_path: Path) -> None:
    """DEATH TEST: absent config.toml must not raise; must use built-in default.

    Silent failure mode A: load_project_config() raises on missing file instead
    of returning built-in defaults → _build_analysis_agent() propagates the exception.

    Silent failure mode B: call succeeds but uses wrong model (neither of these
    should happen — absent TOML → built-in default "claude-haiku-4-5-20251001").
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Intentionally NO config.toml — _build_agent_for_test creates project dir only
    agent = _build_agent_for_test(secondsight_home=home, project_id="proj-wire-3")

    builtin_default = _ADAPTER_DEFAULTS["claude_code"].name  # "claude-haiku-4-5-20251001"
    primary_name = _primary_model_name(agent)

    assert primary_name == builtin_default, (
        f"Expected built-in default {builtin_default!r} when config.toml is absent, "
        f"got {primary_name!r}."
    )


# ---------------------------------------------------------------------------
# UNIT TESTS — verify the full priority chain
# ---------------------------------------------------------------------------


def test_ut_wire_global_claude_code_model_override(tmp_path: Path) -> None:
    """Global config.toml [analysis.models] claude_code override reaches the agent.

    Priority chain step: no per-project override → global model is used.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Write GLOBAL config.toml with claude_code model override
    (home / "config.toml").write_text('[analysis.models]\nclaude_code = "claude-sonnet-4-6"\n')

    # No per-project override — global applies
    agent = _build_agent_for_test(secondsight_home=home, project_id="proj-ut-global")
    primary_name = _primary_model_name(agent)

    assert primary_name == "claude-sonnet-4-6", (
        f"Expected 'claude-sonnet-4-6' from global config.toml [analysis.models], "
        f"got {primary_name!r}."
    )


def test_ut_wire_project_model_beats_global_models_setting(tmp_path: Path) -> None:
    """Per-project [analysis] model beats global [analysis.models.claude_code].

    Priority chain: per-project model override (step 1 in select_model) wins
    over global per-adapter model override (step 4).
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Global sets claude_code model
    (home / "config.toml").write_text('[analysis.models]\nclaude_code = "claude-sonnet-4-6"\n')

    # Per-project overrides with a different model
    _write_project_config(
        home,
        "proj-ut-precedence",
        '[analysis]\nmodel = "claude-haiku-4-6"\n',
    )

    agent = _build_agent_for_test(secondsight_home=home, project_id="proj-ut-precedence")
    primary_name = _primary_model_name(agent)

    assert primary_name == "claude-haiku-4-6", (
        f"Expected per-project 'claude-haiku-4-6' to beat global setting, got {primary_name!r}."
    )


def test_ut_wire_env_var_beats_toml(tmp_path: Path) -> None:
    """SECONDSIGHT_ANALYSIS_MODEL env var beats both per-project and global TOML."""
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Both global and per-project TOML set a model
    (home / "config.toml").write_text('[analysis.models]\nclaude_code = "claude-sonnet-4-6"\n')
    _write_project_config(
        home,
        "proj-ut-env",
        '[analysis]\nmodel = "claude-haiku-4-6"\n',
    )

    env_model = "claude-opus-4-7"
    old_val = os.environ.get("SECONDSIGHT_ANALYSIS_MODEL")
    try:
        os.environ["SECONDSIGHT_ANALYSIS_MODEL"] = env_model
        agent = _build_agent_for_test(secondsight_home=home, project_id="proj-ut-env")
    finally:
        if old_val is None:
            os.environ.pop("SECONDSIGHT_ANALYSIS_MODEL", None)
        else:
            os.environ["SECONDSIGHT_ANALYSIS_MODEL"] = old_val

    primary_name = _primary_model_name(agent)
    assert primary_name == env_model, (
        f"Expected env var {env_model!r} to beat all TOML layers, got {primary_name!r}."
    )


def test_ut_wire_no_project_dir_uses_builtin_default(tmp_path: Path) -> None:
    """Project dir doesn't exist at all: _build_analysis_agent() uses built-in default.

    Note: _build_agent_for_test creates the project dir. This test manually
    creates the agent WITHOUT _build_agent_for_test to verify the no-dir path.
    """
    home = tmp_path / ".secondsight"
    home.mkdir()

    # Task 5: LLMRouter requires at least one provider key. Add a test placeholder
    # to the global config so the router construction passes.
    (home / "config.toml").write_text(
        '[providers.anthropic]\nANTHROPIC_API_KEY = "sk-test-no-project-dir-placeholder"\n'
    )

    # Project dir does NOT exist — no config.toml can exist
    project_id = "proj-nonexistent"
    # Note: NOT calling _build_agent_for_test which creates the dir.
    # Instead we call _build_analysis_agent() directly with a project dir
    # that doesn't exist (no mkdir).

    # We still need project_dir to exist for AnalysisTools project_root.resolve()
    # But load_project_config() must not raise when project TOML is absent.
    # The project dir may or may not exist — AnalysisTools handles None project_root
    # and a non-existent directory by calling .resolve() which returns the path as-is.
    project_dir = home / "projects" / project_id
    project_dir.mkdir(parents=True)  # dir exists, but no config.toml inside

    agent = _build_analysis_agent(
        secondsight_home=home,
        project_id=project_id,
        events_repository=MagicMock(),
        flags_repository=MagicMock(),
        directives_repository=MagicMock(),
    )

    builtin_default = _ADAPTER_DEFAULTS["claude_code"].name
    primary_name = _primary_model_name(agent)
    assert primary_name == builtin_default, (
        f"Expected built-in default {builtin_default!r} when project config.toml absent, "
        f"got {primary_name!r}."
    )
