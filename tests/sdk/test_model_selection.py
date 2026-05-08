"""Tests for sdk/model_selection.py — pure select_model() function.

Samsara discipline: death tests first.

Death cases:
    DT-3.1: missing config raises ModelSelectionError with config-diff suggestion
    DT-3.2: 'auto' with no events → raises ModelSelectionError (fix-loop fix)
    DT-3.3: project override beats global
    DT-3.4: garbage agent_type from events_repo raises ModelSelectionError naming source
    DT-3.5: unknown-case agent_type (e.g. "CLAUDE_CODE") raises ModelSelectionError

Happy paths:
    HP-1.3: project override returns expected ModelSpec
    HP-3.4: 'auto' + recent claude_code session → claude-haiku-4-5
    HP-extra: explicit non-auto default_agent + configured model → that spec

Registry consistency:
    RC-1: ModelsConfig fields must match _ADAPTER_DEFAULTS keys at module load

Migration markers:
    MM-1: EventsRepository.get_latest_session_agent_type always returns None until migration
"""

from __future__ import annotations

from typing import Protocol
from unittest.mock import MagicMock

import pytest

from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.model_selection import (
    ModelSelectionError,
    _KNOWN_AGENT_TYPES,
    select_model,
)


# ---------------------------------------------------------------------------
# Fake EventsRepo
# ---------------------------------------------------------------------------


class _FakeEventsRepo:
    """Minimal fake for injection into select_model.

    Implements the EventsRepoProtocol used by select_model.
    Call .configure(project_id, agent_type) to control return value.
    """

    def __init__(self, agent_type: str | None = None) -> None:
        self._mapping: dict[str, str | None] = {}
        self._default = agent_type

    def configure(self, project_id: str, agent_type: str | None) -> None:
        self._mapping[project_id] = agent_type

    def get_latest_session_agent_type(self, project_id: str) -> str | None:
        if project_id in self._mapping:
            return self._mapping[project_id]
        return self._default


# ---------------------------------------------------------------------------
# Config stubs (minimal dicts-or-objects for injection)
# ---------------------------------------------------------------------------


def _make_global_config(
    *,
    default_agent: str = "claude_code",
    claude_code_model: str = "",
    codex_model: str = "",
    opencode_model: str = "",
    fallback_models: list[str] | None = None,
) -> MagicMock:
    """Build a minimal global config mock matching AnalysisConfig shape."""
    cfg = MagicMock()
    cfg.analysis.default_agent = default_agent
    cfg.analysis.models.claude_code = claude_code_model
    cfg.analysis.models.codex = codex_model
    cfg.analysis.models.opencode = opencode_model
    fb = fallback_models if fallback_models is not None else ["gpt-4o-mini", "gemini-2.0-flash"]
    cfg.analysis.models.fallback.fallback_models = fb
    return cfg


def _make_project_config(*, model: str = "") -> MagicMock:
    """Build a minimal per-project config mock."""
    cfg = MagicMock()
    cfg.analysis.model = model
    return cfg


# ---------------------------------------------------------------------------
# Death tests — target silent failure paths
# ---------------------------------------------------------------------------


class TestDT31MissingConfigRaisesWithSuggestion:
    """DT-3.1: When codex is the resolved agent but its model is not configured,
    ModelSelectionError must include a TOML snippet that would resolve the error.

    Silent failure path: if the function returned a default model without error,
    the wrong model would be silently charged for codex-originated sessions.
    """

    def test_codex_unconfigured_raises_model_selection_error(self) -> None:
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(default_agent="codex", codex_model="")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-alpha", project_cfg, global_cfg, repo)

        err = exc_info.value
        # Error message must contain the config key path
        assert "[analysis.models.codex]" in str(err) or "analysis.models.codex" in str(err)
        # Error must contain a suggested snippet (ready to paste into TOML)
        assert "set analysis.models.codex" in str(err) or (
            hasattr(err, "suggested_config")
            and "analysis.models.codex" in err.suggested_config
        )

    def test_model_selection_error_has_suggested_config_attribute(self) -> None:
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(default_agent="codex", codex_model="")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-alpha", project_cfg, global_cfg, repo)

        err = exc_info.value
        assert hasattr(err, "suggested_config"), (
            "ModelSelectionError must have 'suggested_config' attribute with TOML snippet"
        )
        assert "analysis.models.codex" in err.suggested_config
        # Must include the key that would resolve the error
        assert "<model-name>" in err.suggested_config or "=" in err.suggested_config


class TestDT32AutoWithNoEventsRaisesModelSelectionError:
    """DT-3.2: When default_agent='auto' and events_repo returns no sessions,
    select_model MUST raise ModelSelectionError — not silently substitute claude_code.

    Silent failure path: the old code substituted "claude_code" silently. A
    codex-only user would be billed for Anthropic with no signal. The error must
    name the problem and provide a config snippet to resolve it.
    """

    def test_auto_no_events_raises_model_selection_error(self) -> None:
        """Death test: auto + no sessions → must raise, not silently return claude_code."""
        repo = _FakeEventsRepo(agent_type=None)
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-new", project_cfg, global_cfg, repo)

        err = exc_info.value
        assert "auto" in str(err).lower(), (
            "Error message must mention 'auto' to help operator understand the cause"
        )

    def test_auto_no_events_error_contains_suggested_config(self) -> None:
        """The error must include a TOML snippet showing how to resolve the issue."""
        repo = _FakeEventsRepo(agent_type=None)
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-new", project_cfg, global_cfg, repo)

        err = exc_info.value
        assert hasattr(err, "suggested_config"), (
            "ModelSelectionError for auto+no-events must have suggested_config"
        )
        assert "default_agent" in err.suggested_config, (
            "suggested_config must show how to set an explicit default_agent"
        )

    def test_auto_no_events_error_mentions_inference_returned_none(self) -> None:
        """Error message must indicate that auto-inference returned None."""
        repo = _FakeEventsRepo(agent_type=None)
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-new", project_cfg, global_cfg, repo)

        # Must identify the root cause — no sessions observed
        err_str = str(exc_info.value)
        assert "none" in err_str.lower() or "no session" in err_str.lower(), (
            "Error must state that auto-inference returned None / no session was found"
        )


class TestDT34GarbageAgentTypeRaisesModelSelectionError:
    """DT-3.4: When events_repo returns a garbage agent_type (e.g. 'cursor', 'CLAUDE_CODE'),
    select_model must raise ModelSelectionError naming the source and the closed set.

    Silent failure path: the old code would pass garbage through getattr which returns "",
    then call _resolve_adapter_default with a generic "unknown agent type" error that
    does NOT indicate the value came from events_repo. Operator has no way to diagnose.
    """

    def test_garbage_agent_type_from_events_repo_raises(self) -> None:
        """Death test: events_repo returns 'cursor' → must raise ModelSelectionError."""
        repo = _FakeEventsRepo()
        repo.configure("proj-x", "cursor")  # not in _KNOWN_AGENT_TYPES
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-x", project_cfg, global_cfg, repo)

        err = exc_info.value
        assert "cursor" in str(err), "Error must name the bad value ('cursor')"

    def test_garbage_agent_type_error_names_source(self) -> None:
        """Error must indicate the value came from events_repo, not config."""
        repo = _FakeEventsRepo()
        repo.configure("proj-x", "cursor")
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-x", project_cfg, global_cfg, repo)

        err_str = str(exc_info.value)
        # Must name the source so operator can locate the problem
        assert "events_repo" in err_str or "events repo" in err_str.lower(), (
            "Error must indicate the bad agent_type came from events_repo"
        )

    def test_wrong_case_agent_type_raises(self) -> None:
        """'CLAUDE_CODE' (wrong case) must raise, not silently pass through."""
        repo = _FakeEventsRepo()
        repo.configure("proj-x", "CLAUDE_CODE")  # wrong case
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError):
            select_model("proj-x", project_cfg, global_cfg, repo)

    def test_garbage_agent_type_error_lists_valid_values(self) -> None:
        """Error must list the valid (closed) set so operator can self-serve."""
        repo = _FakeEventsRepo()
        repo.configure("proj-x", "DROP TABLE")
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-x", project_cfg, global_cfg, repo)

        err_str = str(exc_info.value)
        # Must mention at least one known valid value
        assert "claude_code" in err_str, (
            "Error must list valid agent_type values so operator can correct the data"
        )


class TestDT33ProjectOverrideBeatsGlobal:
    """DT-3.3: When project_config.analysis.model is non-empty, it wins
    over global default_agent and global analysis.models.<agent>.

    Silent failure path: if global config silently overrides the project model,
    project-specific cost control settings are ignored, potentially sending
    all project traffic to the wrong (more expensive) model.
    """

    def test_project_model_beats_global_agent_model(self) -> None:
        repo = _FakeEventsRepo()
        # Global says use claude_code → claude-haiku-4-5
        global_cfg = _make_global_config(
            default_agent="claude_code",
            claude_code_model="claude-haiku-4-5",
        )
        # Project says use claude-sonnet-4-6
        project_cfg = _make_project_config(model="claude-sonnet-4-6")

        primary, _ = select_model("proj-alpha", project_cfg, global_cfg, repo)

        assert primary.name == "claude-sonnet-4-6", (
            f"Expected project override 'claude-sonnet-4-6', got '{primary.name}'. "
            "Project config MUST beat global config."
        )

    def test_project_override_events_repo_never_called(self) -> None:
        """When project override is set, events_repo must NOT be consulted
        (pure function: no unnecessary I/O)."""
        repo = MagicMock()
        global_cfg = _make_global_config(default_agent="claude_code")
        project_cfg = _make_project_config(model="claude-sonnet-4-6")

        select_model("proj-alpha", project_cfg, global_cfg, repo)

        repo.get_latest_session_agent_type.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


class TestHP13ProjectOverrideReturnsExpectedModelSpec:
    """HP-1.3: project override returns expected ModelSpec.

    From acceptance.yaml:
    - primary == ModelSpec(name='claude-sonnet-4-6', provider='anthropic')
    - fallbacks == [ModelSpec('gpt-4o-mini', 'openai'), ModelSpec('gemini-2.0-flash', 'google')]
    """

    def test_project_override_primary_spec(self) -> None:
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(
            default_agent="claude_code",
            fallback_models=["gpt-4o-mini", "gemini-2.0-flash"],
        )
        project_cfg = _make_project_config(model="claude-sonnet-4-6")

        primary, fallbacks = select_model("proj-alpha", project_cfg, global_cfg, repo)

        assert primary.name == "claude-sonnet-4-6"
        assert primary.provider == "anthropic"

    def test_project_override_fallback_specs(self) -> None:
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(
            fallback_models=["gpt-4o-mini", "gemini-2.0-flash"],
        )
        project_cfg = _make_project_config(model="claude-sonnet-4-6")

        _, fallbacks = select_model("proj-alpha", project_cfg, global_cfg, repo)

        assert len(fallbacks) == 2
        assert fallbacks[0].name == "gpt-4o-mini"
        assert fallbacks[0].provider == "openai"
        assert fallbacks[1].name == "gemini-2.0-flash"
        assert fallbacks[1].provider == "google"


class TestHP34AutoWithRecentClaudeCodeSession:
    """HP-3.4: 'auto' + recent claude_code session → claude-haiku-4-5.

    events_repo reports the last session used 'claude_code'. select_model
    must look up the SD §5.7.1 default for claude_code.
    """

    def test_auto_with_claude_code_session_returns_haiku(self) -> None:
        repo = _FakeEventsRepo()
        repo.configure("proj-beta", "claude_code")
        global_cfg = _make_global_config(
            default_agent="auto",
            fallback_models=["gpt-4o-mini", "gemini-2.0-flash"],
        )
        project_cfg = _make_project_config(model="")

        primary, _ = select_model("proj-beta", project_cfg, global_cfg, repo)

        assert primary.name == "claude-haiku-4-5-20251001", (
            "auto + recent claude_code session must resolve to SD §5.7.1 default"
        )
        assert primary.provider == "anthropic"

    def test_auto_consults_events_repo_once(self) -> None:
        """Events repo must be called exactly once — not cached in a loop,
        not skipped, not called multiple times."""
        repo = MagicMock()
        repo.get_latest_session_agent_type.return_value = "claude_code"
        global_cfg = _make_global_config(default_agent="auto")
        project_cfg = _make_project_config(model="")

        select_model("proj-beta", project_cfg, global_cfg, repo)

        repo.get_latest_session_agent_type.assert_called_once_with("proj-beta")


class TestHPExtraExplicitNonAutoAgent:
    """HP-extra: explicit non-auto default_agent + configured model → that spec."""

    def test_explicit_codex_with_configured_model(self) -> None:
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(
            default_agent="codex",
            codex_model="gpt-5-codex",
            fallback_models=["gpt-4o-mini"],
        )
        project_cfg = _make_project_config(model="")

        primary, _ = select_model("proj-codex", project_cfg, global_cfg, repo)

        assert primary.name == "gpt-5-codex"

    def test_explicit_claude_code_with_global_model_override(self) -> None:
        """Global [analysis.models.claude_code] beats adapter default when non-empty."""
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(
            default_agent="claude_code",
            claude_code_model="claude-opus-4-5",
            fallback_models=[],
        )
        project_cfg = _make_project_config(model="")

        primary, _ = select_model("proj-alpha", project_cfg, global_cfg, repo)

        assert primary.name == "claude-opus-4-5", (
            "Non-empty global [analysis.models.claude_code] must override adapter default"
        )

    def test_opencode_unconfigured_raises_model_selection_error(self) -> None:
        """opencode has no adapter default (SD §5.7.1: 'requires explicit analysis.model').
        Without a configured model, it must raise ModelSelectionError.
        """
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(
            default_agent="opencode",
            opencode_model="",  # not configured
        )
        project_cfg = _make_project_config(model="")

        with pytest.raises(ModelSelectionError) as exc_info:
            select_model("proj-opencode", project_cfg, global_cfg, repo)

        err = exc_info.value
        assert hasattr(err, "suggested_config")
        assert "analysis" in err.suggested_config.lower() or "model" in err.suggested_config.lower()

    def test_empty_fallbacks_list_is_valid(self) -> None:
        """Empty fallback_models = [] is a valid explicit choice (D13)."""
        repo = _FakeEventsRepo()
        global_cfg = _make_global_config(
            default_agent="claude_code",
            fallback_models=[],
        )
        project_cfg = _make_project_config(model="")

        primary, fallbacks = select_model("proj-alpha", project_cfg, global_cfg, repo)

        assert fallbacks == []
        assert primary.name == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Registry consistency test
# ---------------------------------------------------------------------------


class TestRegistryConsistency:
    """RC-1: ModelsConfig fields must match _ADAPTER_DEFAULTS keys.

    This test fires when the closed set diverges — e.g. a fourth adapter is
    added to ModelsConfig but not _ADAPTER_DEFAULTS, or vice versa.
    """

    def test_models_config_fields_match_adapter_defaults(self) -> None:
        """ModelsConfig fields (excluding 'fallback') must equal _ADAPTER_DEFAULTS keys.

        When this test fails, the developer added an adapter to one place but not
        the other — model selection would silently misroute for the new adapter.
        """
        from dataclasses import fields

        from secondsight.analysis.config import ModelsConfig
        from secondsight.sdk.model_selection import (
            _ADAPTER_DEFAULTS,
            _ADAPTER_ERROR_CONFIGS,
        )

        config_field_names = frozenset(
            f.name for f in fields(ModelsConfig) if f.name != "fallback"
        )
        adapter_keys = frozenset(_ADAPTER_DEFAULTS.keys())

        # Derive raise-only adapters from _ADAPTER_ERROR_CONFIGS.keys() (single source
        # of truth) rather than hardcoding {"codex", "opencode"}. If a new raise-only
        # adapter is added to _ADAPTER_ERROR_CONFIGS but not to ModelsConfig, the
        # assertion below catches it; with a hardcoded literal the test would silently
        # diverge from production.
        raise_only_adapters = frozenset(_ADAPTER_ERROR_CONFIGS.keys())
        expected_config_fields = adapter_keys | raise_only_adapters

        assert config_field_names == expected_config_fields, (
            f"ModelsConfig fields {sorted(config_field_names)} don't match "
            f"_ADAPTER_DEFAULTS keys {sorted(adapter_keys)} + raise-only adapters "
            f"{sorted(raise_only_adapters)}. "
            "When adding a new adapter, update BOTH ModelsConfig and _ADAPTER_DEFAULTS "
            "(or _ADAPTER_ERROR_CONFIGS for raise-only adapters)."
        )

    def test_known_agent_types_includes_all_config_adapters(self) -> None:
        """_KNOWN_AGENT_TYPES must include every adapter that can appear as default_agent."""
        from dataclasses import fields

        from secondsight.analysis.config import ModelsConfig

        config_field_names = frozenset(
            f.name for f in fields(ModelsConfig) if f.name != "fallback"
        )

        for adapter in config_field_names:
            assert adapter in _KNOWN_AGENT_TYPES, (
                f"Adapter '{adapter}' is in ModelsConfig but not in _KNOWN_AGENT_TYPES. "
                "Agent-type validation would reject valid values from config."
            )


# ---------------------------------------------------------------------------
# Schema gap documentation tests
# ---------------------------------------------------------------------------


class TestEventRepoSchemaGap:
    """Documents the current behavior of EventsRepository.get_latest_session_agent_type.

    These tests serve as a migration marker: when the events table gains an
    agent_type column, these tests MUST be updated to use a real SQLite repo
    and verify the actual query behavior.

    See scar report task-3 assumption A1 and events_repository.py docstring.
    """

    def test_get_latest_session_agent_type_with_no_events_returns_none(self) -> None:
        """Current behavior: always returns None (schema gap).

        MIGRATION MARKER: When the events table gains an agent_type column and
        get_latest_session_agent_type is updated to query it, this test must change
        to test the real EventsRepository with a real SQLite DB that has no rows —
        asserting it still returns None for empty tables.

        Additionally, DT-3.2 (TestDT32AutoWithNoEventsRaisesModelSelectionError)
        behavior remains correct: auto + None from repo → ModelSelectionError.
        """
        # Use the fake stub to document the protocol behavior
        fake = _FakeEventsRepo(agent_type=None)
        result = fake.get_latest_session_agent_type("any-project")
        assert result is None

    def test_events_repository_get_latest_session_agent_type_always_returns_none(
        self, tmp_path
    ) -> None:
        """Migration marker: EventsRepository.get_latest_session_agent_type returns None
        until the events table gains an agent_type column.

        When the migration lands:
        1. Remove this test (or replace it with a real-DB test that inserts a session
           with agent_type='claude_code' and asserts the returned value.
        2. Update select_model() auto-mode: remove the ModelSelectionError-on-None path
           and instead use the real agent_type from the repo.
        3. Update DT-3.2 to reflect the new auto-mode behavior.

        If this assertion fails, the schema migration has landed — update all three.
        """
        from secondsight.storage.db_engine import DBEngine
        from secondsight.storage.events_repository import EventsRepository

        db_engine = DBEngine(tmp_path / "test.db")
        try:
            repo = EventsRepository(db_engine)
            repo.create_schema()
            result = repo.get_latest_session_agent_type("any-project")
            assert result is None, (
                "If this assertion fails, the schema migration has landed. "
                "Update select_model() auto-mode to use the real agent_type instead of "
                "the always-None stub. See events_repository.py docstring."
            )
        finally:
            db_engine.dispose()

    def test_model_spec_is_frozen_and_hashable(self) -> None:
        """ModelSpec instances must be hashable (frozen dataclass).

        The router uses ModelSpec instances as dict keys; hashability is required.
        """
        spec1 = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
        spec2 = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
        spec3 = ModelSpec(name="gpt-4o-mini", provider="openai")

        assert spec1 == spec2
        assert hash(spec1) == hash(spec2)
        assert spec1 != spec3
        # Must be usable as a dict key
        d = {spec1: "primary", spec3: "fallback"}
        assert d[spec2] == "primary"  # spec2 == spec1, same hash
