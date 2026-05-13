"""Tests for config/env.py — death tests first.

DT-env-1: SECONDSIGHT_ANALYSIS_MODEL="" (empty string env var) must return None,
    not "". Empty string env var = "not set". If this returns "", the loader in
    task-2 would treat it as a valid model override (a non-empty value in the
    resolution chain), skipping all TOML-layer config. Silent override promotion.

DT-env-2: SECONDSIGHT_DEFAULT_AGENT="" must also return None for the same reason.

Unit tests:
- UT-env-1: get_env_analysis_model() returns value when set.
- UT-env-2: get_env_analysis_model() returns None when unset.
- UT-env-3: get_env_default_agent() returns value when set.
- UT-env-4: get_env_default_agent() returns None when unset.
- UT-env-5: ENV_ANALYSIS_MODEL constant == "SECONDSIGHT_ANALYSIS_MODEL".
- UT-env-6: ENV_DEFAULT_AGENT constant == "SECONDSIGHT_DEFAULT_AGENT".
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Death tests — must fail (red) before env.py is implemented
# ---------------------------------------------------------------------------


class TestDTEnv1EmptyAnalysisModelIsNone:
    """DT-env-1: SECONDSIGHT_ANALYSIS_MODEL="" must return None, not "".

    Silent failure mode: if this returns "", the loader in task-2 treats
    `if model_env_value` as truthy (it won't be), but if the env helper
    accidentally returns "" and the loader checks `if value is not None`
    rather than `if value`, the empty string bypasses all TOML config and
    the user's TOML model setting is silently ignored.

    The env helper MUST normalize "" → None so loaders can use a uniform
    `if value:` check without worrying about the empty-string edge case.
    """

    def test_empty_string_env_var_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("SECONDSIGHT_ANALYSIS_MODEL", "")
        from secondsight.config.env import get_env_analysis_model

        result = get_env_analysis_model()
        assert result is None, (
            f"Expected None for empty SECONDSIGHT_ANALYSIS_MODEL, got {result!r}. "
            "Empty string env var must be treated as 'not set' (normalized to None). "
            "If this returns '', the loader may silently ignore TOML model configuration."
        )

    def test_empty_string_is_not_returned_as_empty_string(self, monkeypatch) -> None:
        """Verify we don't accidentally return "" instead of None."""
        monkeypatch.setenv("SECONDSIGHT_ANALYSIS_MODEL", "")
        from secondsight.config.env import get_env_analysis_model

        result = get_env_analysis_model()
        assert result != "", "Empty env var must NOT be returned as empty string"


class TestDTEnv2EmptyDefaultAgentIsNone:
    """DT-env-2: SECONDSIGHT_DEFAULT_AGENT="" must return None, not "".

    Same silent failure mode as DT-env-1 but for the default_agent env var.
    """

    def test_empty_string_env_var_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("SECONDSIGHT_DEFAULT_AGENT", "")
        from secondsight.config.env import get_env_default_agent

        result = get_env_default_agent()
        assert result is None, (
            f"Expected None for empty SECONDSIGHT_DEFAULT_AGENT, got {result!r}. "
            "Empty string env var must be treated as 'not set' (normalized to None)."
        )


# ---------------------------------------------------------------------------
# Unit tests — verify correct behavior after implementation
# ---------------------------------------------------------------------------


class TestUTEnvAnalysisModel:
    """UT-env-1, UT-env-2: get_env_analysis_model() behavior."""

    def test_returns_value_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("SECONDSIGHT_ANALYSIS_MODEL", "claude-opus-4-5")
        from secondsight.config.env import get_env_analysis_model

        result = get_env_analysis_model()
        assert result == "claude-opus-4-5"

    def test_returns_none_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("SECONDSIGHT_ANALYSIS_MODEL", raising=False)
        from secondsight.config.env import get_env_analysis_model

        result = get_env_analysis_model()
        assert result is None

    def test_returns_value_with_hyphens(self, monkeypatch) -> None:
        """Non-empty model names with hyphens are returned as-is."""
        monkeypatch.setenv("SECONDSIGHT_ANALYSIS_MODEL", "my-model")
        from secondsight.config.env import get_env_analysis_model

        result = get_env_analysis_model()
        assert result == "my-model"

    def test_whitespace_only_returns_none(self, monkeypatch) -> None:
        """sfc-3 fix: whitespace-only env var must return None, not ' '.

        A value of SECONDSIGHT_ANALYSIS_MODEL='   ' would be truthy (passes
        `if not value` check on raw) but after stripping is empty, so the
        loader would receive None rather than ' ' as a model name.
        """
        monkeypatch.setenv("SECONDSIGHT_ANALYSIS_MODEL", "   ")
        from secondsight.config.env import get_env_analysis_model

        result = get_env_analysis_model()
        assert result is None, (
            f"Whitespace-only env var must return None, got {result!r}. "
            "A whitespace-only model name would cause ModelSelectionError or "
            "LiteLLM error at analysis time rather than at config load time."
        )


class TestUTEnvDefaultAgent:
    """UT-env-3, UT-env-4: get_env_default_agent() behavior."""

    def test_returns_value_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("SECONDSIGHT_DEFAULT_AGENT", "codex")
        from secondsight.config.env import get_env_default_agent

        result = get_env_default_agent()
        assert result == "codex"

    def test_returns_none_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("SECONDSIGHT_DEFAULT_AGENT", raising=False)
        from secondsight.config.env import get_env_default_agent

        result = get_env_default_agent()
        assert result is None


class TestUTEnvConstants:
    """UT-env-5, UT-env-6: ENV_* constants have correct string values."""

    def test_env_analysis_model_constant(self) -> None:
        from secondsight.config.env import ENV_ANALYSIS_MODEL

        assert ENV_ANALYSIS_MODEL == "SECONDSIGHT_ANALYSIS_MODEL"

    def test_env_default_agent_constant(self) -> None:
        from secondsight.config.env import ENV_DEFAULT_AGENT

        assert ENV_DEFAULT_AGENT == "SECONDSIGHT_DEFAULT_AGENT"

    def test_constants_are_strings(self) -> None:
        from secondsight.config.env import ENV_ANALYSIS_MODEL, ENV_DEFAULT_AGENT

        assert isinstance(ENV_ANALYSIS_MODEL, str)
        assert isinstance(ENV_DEFAULT_AGENT, str)

    def test_constants_match_what_helpers_read(self, monkeypatch) -> None:
        """Helpers must read the exact env var named by their constant."""
        from secondsight.config.env import ENV_ANALYSIS_MODEL, ENV_DEFAULT_AGENT
        from secondsight.config.env import get_env_analysis_model, get_env_default_agent

        monkeypatch.setenv(ENV_ANALYSIS_MODEL, "test-model")
        assert get_env_analysis_model() == "test-model"

        monkeypatch.setenv(ENV_DEFAULT_AGENT, "claude_code")
        assert get_env_default_agent() == "claude_code"
