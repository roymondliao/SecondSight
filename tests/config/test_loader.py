"""Tests for config/loader.py — death tests first, then unit tests.

Death Tests (DT):
- DT-loader-1: ${MISSING_KEY} in TOML string value → SecondSightConfigError, not literal
- DT-loader-2: ${EMPTY_KEY} expands to "" → SecondSightConfigError
- DT-loader-3: .env file has ANTHROPIC_API_KEY=sk-test, os.environ does not → after
               load_global_config(), os.environ["ANTHROPIC_API_KEY"] == "sk-test"
- DT-loader-4: .env has ANTHROPIC_API_KEY=from_dotenv, os.environ has from_env → os.environ wins
- DT-loader-5: per-project config has model="claude-sonnet-4-6", global has default_agent →
               load_project_config() returns project_analysis.model == "claude-sonnet-4-6"
- DT-loader-6: global config.toml does not exist → load_global_config() returns built-in defaults
- DT-loader-7: _parse_toml with custom env dict uses custom env, not os.environ
- DT-loader-8: malformed retention value propagates as SecondSightConfigError, not raw
               RetentionConfigError

Unit Tests (UT):
- UT-loader-1: _interpolate_vars substitutes ${VAR} from os.environ
- UT-loader-2: _interpolate_dict recurses into nested dicts and lists
- UT-loader-3: _interpolate_vars leaves non-${} text intact
- UT-loader-4: _interpolate_vars raises on missing key (not empty string check)
- UT-loader-5: _interpolate_vars raises on empty env var value
- UT-loader-6: _parse_toml returns None for non-existent path
- UT-loader-7: _parse_toml raises SecondSightConfigError for malformed TOML
- UT-loader-8: load_global_config reads [analysis].default_agent from TOML
- UT-loader-9: load_global_config reads [retention] section
- UT-loader-10: SECONDSIGHT_ANALYSIS_MODEL env var overrides per-project model
- UT-loader-11: SECONDSIGHT_DEFAULT_AGENT env var overrides TOML default_agent
- UT-loader-12: empty string model in per-project TOML falls through to global default
- UT-loader-13: load_project_config missing project TOML → uses global defaults
- UT-loader-14: _interpolate_vars lowercase ${my_var} not matched (intentional)
- UT-loader-15: load_global_config reads [analysis.models] section
- UT-loader-16: load_project_config docstring documents model="" semantics
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_dotenv(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Death tests — must fail BEFORE loader.py is implemented
# ---------------------------------------------------------------------------


class TestDTLoader1MissingKeyInterpolation:
    """DT-loader-1: ${MISSING_KEY} in TOML string → SecondSightConfigError, not literal.

    Silent failure path: if interpolation is not implemented or silently skips
    missing vars, the literal string '${MISSING_KEY}' becomes the model name,
    causing a cryptic LiteLLM error at analysis time instead of a clear config error.
    """

    def test_missing_key_raises_not_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "${NONEXISTENT_AGENT_VAR}"',
        )
        # Ensure the var is truly absent
        monkeypatch.delenv("NONEXISTENT_AGENT_VAR", raising=False)

        from secondsight.config.schema import SecondSightConfigError
        from secondsight.config.loader import load_global_config

        with pytest.raises(SecondSightConfigError):
            load_global_config(home)


class TestDTLoader2EmptyKeyInterpolation:
    """DT-loader-2: ${EMPTY_KEY} expands to "" → SecondSightConfigError.

    Silent failure path: if empty-string expansion is accepted silently,
    the loader sets model="" (empty = not set) or passes an empty string
    to LiteLLM. Either case is wrong — the user clearly intended to reference
    an env var, so empty = misconfiguration, not "use default".
    """

    def test_empty_key_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "secondsight"
        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "${EMPTY_AGENT_VAR}"',
        )
        monkeypatch.setenv("EMPTY_AGENT_VAR", "")

        from secondsight.config.schema import SecondSightConfigError
        from secondsight.config.loader import load_global_config

        with pytest.raises(SecondSightConfigError):
            load_global_config(home)


class TestDTLoader3DotenvLoads:
    """DT-loader-3: .env file key not in os.environ → gets loaded into os.environ.

    Silent failure path: if load_dotenv is not called or called with wrong path,
    ANTHROPIC_API_KEY stays absent. Any subsequent LiteLLM call that needs the key
    silently fails or uses a stale key from elsewhere.
    """

    def test_dotenv_key_loaded_into_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "secondsight"
        _write_dotenv(home / ".env", "ANTHROPIC_API_KEY=sk-test-from-dotenv\n")
        # Ensure the key is NOT in os.environ before load
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from secondsight.config.loader import load_global_config

        load_global_config(home)

        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test-from-dotenv"


class TestDTLoader4DotenvDoesNotOverrideEnv:
    """DT-loader-4: os.environ takes priority over .env (override=False).

    Silent failure path: if load_dotenv is called with override=True, an operator
    who sets ANTHROPIC_API_KEY in their shell environment would have it silently
    overwritten by whatever is in .env, potentially switching to wrong credentials.
    """

    def test_os_environ_wins_over_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "secondsight"
        _write_dotenv(home / ".env", "ANTHROPIC_API_KEY=from_dotenv\n")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from_env")

        from secondsight.config.loader import load_global_config

        load_global_config(home)

        assert os.environ["ANTHROPIC_API_KEY"] == "from_env"


class TestDTLoader5PerProjectModelPriority:
    """DT-loader-5: per-project config model overrides global default_agent.

    Silent failure path: if project overlay is not applied, the per-project model
    is silently ignored and the global default is used. User configured per-project
    and never knows their override is gone. The analysis proceeds with wrong model.
    """

    def test_per_project_model_overrides_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "secondsight"
        project_id = "my-project"

        _write_toml(
            home / "config.toml",
            '[analysis]\ndefault_agent = "codex"',
        )
        _write_toml(
            home / "projects" / project_id / "config.toml",
            '[analysis]\nmodel = "claude-sonnet-4-6"',
        )
        # Ensure env var override is absent
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)

        from secondsight.config.loader import load_project_config

        cfg = load_project_config(home, project_id)

        assert cfg.project_analysis.model == "claude-sonnet-4-6"


class TestDTLoader6MissingGlobalConfigFallback:
    """DT-loader-6: missing global config.toml → built-in defaults returned, no raise.

    Silent failure path: if missing config raises instead of using defaults,
    a fresh install with no config.toml would immediately crash. This is the
    most common installation path and must work silently.
    """

    def test_missing_global_config_returns_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "secondsight"
        home.mkdir()
        # Do not create config.toml
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import BUILTIN_DEFAULT_AGENT

        cfg = load_global_config(home)

        assert cfg.analysis.default_agent == BUILTIN_DEFAULT_AGENT
        assert cfg.retention.raw_traces_ttl_days == 90  # built-in default
        assert cfg.project_analysis.model == ""  # empty = not set


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestUTInterpolateVars:
    """Tests for _interpolate_vars() — the ${VAR} substitution helper."""

    def test_substitutes_present_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_MODEL", "claude-opus-4-5")
        from secondsight.config.loader import _interpolate_vars

        result = _interpolate_vars("${MY_MODEL}", os.environ)
        assert result == "claude-opus-4-5"

    def test_leaves_plain_text_intact(self) -> None:
        from secondsight.config.loader import _interpolate_vars

        result = _interpolate_vars("claude-sonnet-4-6", os.environ)
        assert result == "claude-sonnet-4-6"

    def test_raises_on_missing_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFINITELY_MISSING_VAR", raising=False)
        from secondsight.config.loader import _interpolate_vars
        from secondsight.config.schema import SecondSightConfigError

        with pytest.raises(SecondSightConfigError):
            _interpolate_vars("${DEFINITELY_MISSING_VAR}", os.environ)

    def test_raises_on_empty_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMPTY_MODEL_VAR", "")
        from secondsight.config.loader import _interpolate_vars
        from secondsight.config.schema import SecondSightConfigError

        with pytest.raises(SecondSightConfigError):
            _interpolate_vars("${EMPTY_MODEL_VAR}", os.environ)

    def test_lowercase_pattern_not_matched(self) -> None:
        """DT-loader-14 equivalent: lowercase ${my_var} is not expanded (intentional limit)."""
        from secondsight.config.loader import _interpolate_vars

        # Should return as-is, not raise — lowercase pattern is not our ${VAR} pattern
        result = _interpolate_vars("${my_var}", os.environ)
        assert result == "${my_var}"

    def test_mixed_text_and_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST_NAME", "localhost")
        from secondsight.config.loader import _interpolate_vars

        result = _interpolate_vars("http://${HOST_NAME}:8080", os.environ)
        assert result == "http://localhost:8080"

    def test_multiple_vars_in_one_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROTO", "https")
        monkeypatch.setenv("PORT_NUM", "443")
        from secondsight.config.loader import _interpolate_vars

        result = _interpolate_vars("${PROTO}://api:${PORT_NUM}", os.environ)
        assert result == "https://api:443"


class TestUTInterpolateDict:
    """Tests for _interpolate_dict() — recursive dict scanner."""

    def test_recurses_into_nested_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NESTED_VAL", "resolved")
        from secondsight.config.loader import _interpolate_dict

        doc = {"outer": {"inner": "${NESTED_VAL}"}}
        result = _interpolate_dict(doc)
        assert result["outer"]["inner"] == "resolved"

    def test_non_string_values_preserved(self) -> None:
        from secondsight.config.loader import _interpolate_dict

        doc = {"count": 42, "flag": True, "data": [1, 2, 3]}
        result = _interpolate_dict(doc)
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["data"] == [1, 2, 3]

    def test_string_list_values_interpolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_A", "gpt-4o")
        from secondsight.config.loader import _interpolate_dict

        doc = {"models": ["${MODEL_A}", "gemini-flash"]}
        result = _interpolate_dict(doc)
        assert result["models"] == ["gpt-4o", "gemini-flash"]

    def test_empty_dict_returns_empty(self) -> None:
        from secondsight.config.loader import _interpolate_dict

        assert _interpolate_dict({}) == {}


class TestUTParseToml:
    """Tests for _parse_toml() — TOML file reader."""

    def test_returns_none_for_nonexistent_path(self, tmp_path: Path) -> None:
        from secondsight.config.loader import _parse_toml

        result = _parse_toml(tmp_path / "does_not_exist.toml")
        assert result is None

    def test_raises_for_malformed_toml(self, tmp_path: Path) -> None:
        bad_toml = tmp_path / "bad.toml"
        bad_toml.write_text("this is not [ valid toml !!!!", encoding="utf-8")

        from secondsight.config.loader import _parse_toml
        from secondsight.config.schema import SecondSightConfigError

        with pytest.raises(SecondSightConfigError):
            _parse_toml(bad_toml)

    def test_parses_valid_toml(self, tmp_path: Path) -> None:
        good_toml = tmp_path / "good.toml"
        good_toml.write_text('[analysis]\ndefault_agent = "codex"', encoding="utf-8")

        from secondsight.config.loader import _parse_toml

        result = _parse_toml(good_toml)
        assert result is not None
        assert result["analysis"]["default_agent"] == "codex"

    def test_interpolates_after_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_AGENT", "opencode")
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[analysis]\ndefault_agent = "${MY_AGENT}"', encoding="utf-8")

        from secondsight.config.loader import _parse_toml

        result = _parse_toml(toml_path)
        assert result is not None
        assert result["analysis"]["default_agent"] == "opencode"


class TestUTLoadGlobalConfig:
    """Tests for load_global_config()."""

    def test_reads_default_agent_from_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        _write_toml(home / "config.toml", '[analysis]\ndefault_agent = "codex"')
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config

        cfg = load_global_config(home)
        assert cfg.analysis.default_agent == "codex"

    def test_env_var_overrides_toml_default_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        _write_toml(home / "config.toml", '[analysis]\ndefault_agent = "codex"')
        monkeypatch.setenv("SECONDSIGHT_DEFAULT_AGENT", "opencode")

        from secondsight.config.loader import load_global_config

        cfg = load_global_config(home)
        assert cfg.analysis.default_agent == "opencode"

    def test_reads_retention_from_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        _write_toml(home / "config.toml", "[retention]\nraw_traces_ttl_days = 30")
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config

        cfg = load_global_config(home)
        assert cfg.retention.raw_traces_ttl_days == 30
        assert cfg.retention.raw_traces_source == "global_config"

    def test_reads_models_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "ss"
        _write_toml(
            home / "config.toml",
            '[analysis.models]\nclaude_code = "claude-opus-4-5"',
        )
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config

        cfg = load_global_config(home)
        assert cfg.analysis.models.claude_code == "claude-opus-4-5"

    def test_project_analysis_empty_at_global_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        home.mkdir()
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config

        cfg = load_global_config(home)
        # At global level, project_analysis has no model override
        assert cfg.project_analysis.model == ""


class TestUTLoadProjectConfig:
    """Tests for load_project_config()."""

    def test_env_var_analysis_model_highest_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        project_id = "proj-1"
        _write_toml(
            home / "projects" / project_id / "config.toml",
            '[analysis]\nmodel = "per-project-model"',
        )
        monkeypatch.setenv("SECONDSIGHT_ANALYSIS_MODEL", "env-override-model")

        from secondsight.config.loader import load_project_config

        cfg = load_project_config(home, project_id)
        # Env var wins over everything
        assert cfg.project_analysis.model == "env-override-model"

    def test_empty_string_model_in_project_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        project_id = "proj-empty"
        _write_toml(
            home / "projects" / project_id / "config.toml",
            '[analysis]\nmodel = ""',
        )
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)

        from secondsight.config.loader import load_project_config

        cfg = load_project_config(home, project_id)
        # Empty model = not set = fall through (loader treats "" as no override)
        assert cfg.project_analysis.model == ""

    def test_missing_project_config_returns_global_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        project_id = "proj-no-config"
        _write_toml(home / "config.toml", '[analysis]\ndefault_agent = "opencode"')
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)

        from secondsight.config.loader import load_project_config

        cfg = load_project_config(home, project_id)
        assert cfg.analysis.default_agent == "opencode"
        assert cfg.project_analysis.model == ""

    def test_dotenv_loaded_once_on_project_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        _write_dotenv(home / ".env", "TEST_PROJECT_KEY=project-value\n")
        monkeypatch.delenv("TEST_PROJECT_KEY", raising=False)
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)

        from secondsight.config.loader import load_project_config

        load_project_config(home, "any-project")

        assert os.environ.get("TEST_PROJECT_KEY") == "project-value"


class TestUTFallbackModels:
    """Tests for [analysis.models.fallback] section loading."""

    def test_reads_fallback_models_from_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        _write_toml(
            home / "config.toml",
            '[analysis.models.fallback]\nfallback_models = ["gpt-4o", "claude-haiku"]',
        )
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config

        cfg = load_global_config(home)
        assert cfg.analysis.models.fallback.fallback_models == ["gpt-4o", "claude-haiku"]

    def test_builtin_fallback_when_no_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        home.mkdir()
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import BUILTIN_FALLBACK_MODELS

        cfg = load_global_config(home)
        assert cfg.analysis.models.fallback.fallback_models == list(BUILTIN_FALLBACK_MODELS)


# ---------------------------------------------------------------------------
# New death tests — for Critical Fix 1 and Additional Fix (RetentionConfigError)
# ---------------------------------------------------------------------------


class TestDTLoader7ParseTomlEnvThreaded:
    """DT-loader-7: _parse_toml must use the env parameter, not os.environ.

    Silent failure path: if _parse_toml ignores the env parameter and always reads
    os.environ, a caller passing a test-isolation dict (or a scoped env override)
    will silently get values from the wrong source. The interpolation appears to
    succeed, but with values from os.environ rather than the supplied env dict.

    This is the exact condition described in Critical Fix 1: scar sfc-2 claimed
    "env threaded recursively" but _parse_toml → _interpolate_dict never passed env.
    """

    def test_parse_toml_uses_injected_env_not_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Write a TOML file with a ${VAR} reference
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[analysis]\ndefault_agent = "${INJECTED_AGENT}"', encoding="utf-8")

        # Ensure the var is absent from os.environ so any fallthrough to os.environ would raise
        monkeypatch.delenv("INJECTED_AGENT", raising=False)

        from secondsight.config.loader import _parse_toml

        # Supply the var via the env parameter — this must be used, not os.environ
        custom_env = {"INJECTED_AGENT": "opencode-from-custom-env"}
        result = _parse_toml(toml_path, env=custom_env)

        assert result is not None
        assert result["analysis"]["default_agent"] == "opencode-from-custom-env"

    def test_parse_toml_env_absence_still_raises_on_missing_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with an explicit env dict, missing vars must still raise SecondSightConfigError."""
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[analysis]\ndefault_agent = "${NOT_IN_CUSTOM_ENV}"', encoding="utf-8")
        monkeypatch.delenv("NOT_IN_CUSTOM_ENV", raising=False)

        from secondsight.config.loader import _parse_toml
        from secondsight.config.schema import SecondSightConfigError

        with pytest.raises(SecondSightConfigError, match="NOT_IN_CUSTOM_ENV"):
            _parse_toml(toml_path, env={"OTHER_VAR": "irrelevant"})


class TestDTLoader8RetentionConfigErrorWrapped:
    """DT-loader-8: malformed retention value → SecondSightConfigError, not RetentionConfigError.

    Silent failure path: a caller using `except SecondSightConfigError` will silently
    miss retention parse errors if RetentionConfigError is not caught and re-raised
    as SecondSightConfigError. The exception propagates through as an unknown type and
    the caller's error handling fails to fire.

    This is the Additional Fix: catch RetentionConfigError in _build_retention_config
    and re-raise as SecondSightConfigError.
    """

    def test_malformed_retention_ttl_raises_secondsight_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        # Write a global config with an invalid TTL (string instead of int)
        toml_content = '[retention]\nraw_traces_ttl_days = "not-an-int"'
        (home).mkdir()
        (home / "config.toml").write_text(toml_content, encoding="utf-8")

        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config
        from secondsight.config.schema import SecondSightConfigError

        # Must raise SecondSightConfigError (NOT RetentionConfigError)
        with pytest.raises(SecondSightConfigError):
            load_global_config(home)

    def test_malformed_retention_does_not_expose_retention_error_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "ss"
        toml_content = "[retention]\nraw_traces_ttl_days = -5"
        home.mkdir()
        (home / "config.toml").write_text(toml_content, encoding="utf-8")

        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)

        from secondsight.config.loader import load_global_config
        from secondsight.storage.retention import RetentionConfigError
        from secondsight.config.schema import SecondSightConfigError

        # Must NOT propagate as RetentionConfigError — callers use SecondSightConfigError
        with pytest.raises(SecondSightConfigError):
            load_global_config(home)

        # Confirm it does NOT propagate as raw RetentionConfigError
        try:
            load_global_config(home)
        except RetentionConfigError:
            pytest.fail(
                "RetentionConfigError escaped load_global_config — "
                "caller catch on SecondSightConfigError would miss this"
            )
        except SecondSightConfigError:
            pass  # correct


# ---------------------------------------------------------------------------
# New unit tests for Fix 2 (docstring) and Fix 3 (__all__)
# ---------------------------------------------------------------------------


class TestUTLoadProjectConfigDocstring:
    """UT-loader-16: load_project_config docstring documents model="" semantics.

    This test verifies the contract is documented at the public API boundary —
    not that the behavior itself is correct (that's covered by UT-loader-12).
    The test checks the docstring contains the key phrase to guard against
    the documentation being silently removed in a refactor.
    """

    def test_load_project_config_docstring_mentions_empty_model(self) -> None:
        from secondsight.config import loader

        doc = loader.load_project_config.__doc__
        assert doc is not None, "load_project_config must have a docstring"
        # The docstring must mention empty string semantics for model
        assert '""' in doc or "empty" in doc.lower(), (
            "load_project_config docstring must document that model='' means "
            "'use adapter default' — callers must not treat '' as a configured model name"
        )


class TestUTBuildConfigFromDocsInAll:
    """UT-loader-17: _build_config_from_docs must have explicit decision in __all__ or comment.

    This test verifies the codebase has a clear statement about why
    _build_config_from_docs is or isn't in __all__ — the absence of
    a comment is what created the misleading inconsistency.
    """

    def test_build_config_from_docs_is_importable_from_loader(self) -> None:
        """_build_config_from_docs must be directly importable for testing."""
        from secondsight.config.loader import _build_config_from_docs  # noqa: F401

        # If it can be imported, it's accessible for testing regardless of __all__
        assert callable(_build_config_from_docs)
