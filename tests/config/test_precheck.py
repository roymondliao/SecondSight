"""Death tests for precheck() — Task 6 startup pre-check.

Death tests MUST come first — each targets a silent failure path.

Death case reference (from task-6.md):
  DC5: state.json missing or binary not in PATH → precheck fail at startup
  DC6: CLI binary resolved at startup; path captured in INFO log for forensics
  DC7: No providers configured for SDK mode → precheck fail
  DC-PRIMARY-MISSING: mode=sdk, primary_model empty → precheck fail
  DC-OPENCODE: mode=cli, default_agent="opencode" → precheck fail

The key invariant: precheck() returns PrecheckResult, never raises.
All bad config states produce PrecheckResult.fail(...), not exceptions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from secondsight.config.schema import (
    AnalysisCLIConfig,
    AnalysisCLIModelsConfig,
    AnalysisConfig,
    AnalysisSDKConfig,
    GeneralConfig,
    GlobalAnalysisConfig,
    ProjectAnalysisConfig,
    ProviderAnthropicConfig,
    ProviderCustomConfig,
    ProviderOpenAIConfig,
    ProvidersConfig,
    SecondSightConfig,
)
from secondsight.state import SecondSightState
from secondsight.storage.retention import RetentionConfig


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_retention() -> RetentionConfig:
    return RetentionConfig(
        raw_traces_ttl_days=30,
        raw_traces_source="builtin_default",
        analysis_ttl_days=90,
        analysis_ttl_source="builtin_default",
        cleanup_after_analysis=False,
    )


def _make_cli_config(
    mode: str = "cli",
    default_agent: str = "auto",
) -> SecondSightConfig:
    return SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode=mode),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY=""),
            openai=ProviderOpenAIConfig(OPENAI_API_KEY=""),
            custom=ProviderCustomConfig(API_KEY="", base_url=""),
        ),
        analysis=AnalysisConfig(
            timeout_seconds=300,
            cli=AnalysisCLIConfig(
                default_agent=default_agent,
                models=AnalysisCLIModelsConfig(),
            ),
            sdk=AnalysisSDKConfig(
                primary_model="claude-haiku-4-5-20251001",
                fallback_model="gpt-4o-mini",
            ),
        ),
        analysis_global=GlobalAnalysisConfig(),
        project_analysis=ProjectAnalysisConfig(),
    )


def _make_sdk_config(
    primary_model: str = "claude-haiku-4-5-20251001",
    anthropic_key: str = "sk-ant-test-key",
    openai_key: str = "",
) -> SecondSightConfig:
    return SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode="sdk"),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY=anthropic_key),
            openai=ProviderOpenAIConfig(OPENAI_API_KEY=openai_key),
            custom=ProviderCustomConfig(API_KEY="", base_url=""),
        ),
        analysis=AnalysisConfig(
            timeout_seconds=300,
            cli=AnalysisCLIConfig(),
            sdk=AnalysisSDKConfig(
                primary_model=primary_model,
                fallback_model="gpt-4o-mini",
            ),
        ),
        analysis_global=GlobalAnalysisConfig(),
        project_analysis=ProjectAnalysisConfig(),
    )


def _make_state(agent: str = "claude_code") -> SecondSightState:
    return SecondSightState(
        schema_version="1.0",
        init_agent=agent,
        init_at="2026-05-14T00:00:00+00:00",
        secondsight_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# DEATH TESTS — DC5: state.json missing for mode=cli, default_agent=auto
# ---------------------------------------------------------------------------


def test_dc5_cli_auto_state_missing_returns_fail_state_missing() -> None:
    """DC5: mode=cli, default_agent="auto", state.json missing → fail(reason=state_missing)."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "state_missing"
    assert "secondsight init" in result.message.lower() or "init" in result.message


def test_dc5_cli_auto_state_missing_fail_message_is_actionable() -> None:
    """DC5: failure message must tell the user what to do (mention 'init' or agent)."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    result = precheck(config=config, state=None)

    assert not result.is_ok
    # The message must be actionable, not just "state missing"
    assert len(result.message) > 10


# ---------------------------------------------------------------------------
# DEATH TESTS — DC5: binary not in PATH for mode=cli
# ---------------------------------------------------------------------------


def test_dc5_cli_claude_code_binary_missing_returns_fail_binary_missing() -> None:
    """DC5: mode=cli, default_agent="auto", state.init_agent="claude_code", claude binary not found."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="claude_code")

    # Patch shutil.which to return None for ALL CLI binary lookups
    with patch("shutil.which", return_value=None):
        result = precheck(config=config, state=state)

    assert not result.is_ok
    assert result.reason == "cli_binary_missing"
    # Message must name the binary that was looked for
    assert "claude" in result.message.lower() or "binary" in result.message.lower()


def test_dc5_cli_explicit_claude_code_binary_missing() -> None:
    """DC5: mode=cli, default_agent="claude_code" (explicit, not auto), binary missing."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="claude_code")
    # No state needed for explicit agent
    with patch("shutil.which", return_value=None):
        result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "cli_binary_missing"


def test_dc5_cli_codex_binary_missing() -> None:
    """DC5: mode=cli, default_agent="codex", codex binary missing."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="codex")
    with patch("shutil.which", return_value=None):
        result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "cli_binary_missing"


# ---------------------------------------------------------------------------
# DEATH TESTS — DC5: opencode not supported
# ---------------------------------------------------------------------------


def test_dc5_cli_opencode_explicit_fails_opencode_not_supported() -> None:
    """DC5: mode=cli, default_agent="opencode" → fail(reason=opencode_not_supported)."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="opencode")
    result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "opencode_not_supported"
    assert "opencode" in result.message.lower()


def test_dc5_cli_opencode_via_state_auto_resolution_fails() -> None:
    """DC5: mode=cli, default_agent="auto", state.init_agent="opencode" → opencode_not_supported."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="opencode")
    result = precheck(config=config, state=state)

    assert not result.is_ok
    assert result.reason == "opencode_not_supported"


# ---------------------------------------------------------------------------
# DEATH TESTS — DC7: mode=sdk, no providers configured
# ---------------------------------------------------------------------------


def test_dc7_sdk_all_providers_empty_returns_fail_no_providers() -> None:
    """DC7: mode=sdk, all provider keys empty → fail(reason=no_providers)."""
    from secondsight.config.precheck import precheck

    # Also clear custom
    config_no_providers = SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode="sdk"),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY=""),
            openai=ProviderOpenAIConfig(OPENAI_API_KEY=""),
            custom=ProviderCustomConfig(API_KEY="", base_url=""),
        ),
        analysis=AnalysisConfig(
            timeout_seconds=300,
            cli=AnalysisCLIConfig(),
            sdk=AnalysisSDKConfig(
                primary_model="claude-haiku-4-5-20251001",
                fallback_model="gpt-4o-mini",
            ),
        ),
        analysis_global=GlobalAnalysisConfig(),
        project_analysis=ProjectAnalysisConfig(),
    )

    result = precheck(config=config_no_providers, state=None)

    assert not result.is_ok
    assert result.reason == "no_providers"
    # Message should hint at ${VAR} env injection
    assert "provider" in result.message.lower() or "key" in result.message.lower()


def test_dc7_sdk_fail_message_hints_at_env_injection() -> None:
    """DC7 failure message must hint at ${ANTHROPIC_API_KEY} syntax."""
    from secondsight.config.precheck import precheck

    config = SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode="sdk"),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY=""),
            openai=ProviderOpenAIConfig(OPENAI_API_KEY=""),
            custom=ProviderCustomConfig(API_KEY="", base_url=""),
        ),
        analysis=AnalysisConfig(
            timeout_seconds=300,
            cli=AnalysisCLIConfig(),
            sdk=AnalysisSDKConfig(primary_model="claude-haiku-4-5-20251001"),
        ),
        analysis_global=GlobalAnalysisConfig(),
        project_analysis=ProjectAnalysisConfig(),
    )

    result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "no_providers"
    # Must mention environment variable injection pattern
    assert (
        "${" in result.message or "env" in result.message.lower() or "ANTHROPIC" in result.message
    )


# ---------------------------------------------------------------------------
# DEATH TESTS — primary_model missing for mode=sdk
# ---------------------------------------------------------------------------


def test_dc_primary_model_missing_returns_fail_primary_model_missing() -> None:
    """mode=sdk, primary_model is empty string → fail(reason=primary_model_missing)."""
    from secondsight.config.precheck import precheck

    config = _make_sdk_config(
        primary_model="",  # empty → missing
        anthropic_key="sk-ant-test-key",
    )
    result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "primary_model_missing"


# ---------------------------------------------------------------------------
# DEATH TESTS — DC6: binary path logged at INFO level on success
# ---------------------------------------------------------------------------


def test_dc6_cli_success_logs_resolved_binary_path(caplog: pytest.LogCaptureFixture) -> None:
    """DC6 forensics: on precheck success for cli mode, INFO log contains resolved binary path."""
    import logging

    from secondsight.config.precheck import precheck

    fake_binary_path = "/usr/local/bin/claude"

    config = _make_cli_config(mode="cli", default_agent="claude_code")

    with patch("shutil.which", return_value=fake_binary_path):
        with caplog.at_level(logging.INFO):
            result = precheck(config=config, state=None)

    assert result.is_ok, f"Expected precheck to pass, got: {result.reason} - {result.message}"
    # The binary path must appear in at least one log record
    all_messages = " ".join(r.message for r in caplog.records)
    assert fake_binary_path in all_messages, (
        f"Expected resolved binary path {fake_binary_path!r} in INFO log. "
        f"Got log messages: {all_messages!r}"
    )


def test_dc6_cli_auto_resolved_state_logs_binary_path(caplog: pytest.LogCaptureFixture) -> None:
    """DC6 forensics: auto resolution also logs binary path."""
    import logging

    from secondsight.config.precheck import precheck

    fake_binary_path = "/usr/bin/claude"
    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="claude_code")

    with patch("shutil.which", return_value=fake_binary_path):
        with caplog.at_level(logging.INFO):
            result = precheck(config=config, state=state)

    assert result.is_ok, f"Expected precheck to pass, got: {result.reason} - {result.message}"
    all_messages = " ".join(r.message for r in caplog.records)
    assert fake_binary_path in all_messages


# ---------------------------------------------------------------------------
# UNIT TESTS — happy paths
# ---------------------------------------------------------------------------


def test_precheck_result_ok_factory() -> None:
    """PrecheckResult.ok() creates a result where is_ok is True."""
    from secondsight.config.precheck import PrecheckResult

    result = PrecheckResult.ok()
    assert result.is_ok
    assert result.reason is None
    assert result.message == "" or result.message is None or result.message == "ok"


def test_precheck_result_fail_factory() -> None:
    """PrecheckResult.fail() creates a result where is_ok is False with populated reason/message."""
    from secondsight.config.precheck import PrecheckResult

    result = PrecheckResult.fail(reason="some_reason", message="some message")
    assert not result.is_ok
    assert result.reason == "some_reason"
    assert result.message == "some message"


def test_precheck_cli_happy_path_with_binary() -> None:
    """mode=cli, default_agent="claude_code", binary found → precheck passes."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="claude_code")

    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        result = precheck(config=config, state=None)

    assert result.is_ok


def test_precheck_sdk_happy_path_with_anthropic_key() -> None:
    """mode=sdk, primary_model set, anthropic key set → precheck passes."""
    from secondsight.config.precheck import precheck

    config = _make_sdk_config(
        primary_model="claude-haiku-4-5-20251001",
        anthropic_key="sk-ant-test-key",
    )
    result = precheck(config=config, state=None)

    assert result.is_ok


def test_precheck_sdk_happy_path_with_openai_key_only() -> None:
    """mode=sdk, only openai key set → precheck passes (at least one provider is enough)."""
    from secondsight.config.precheck import precheck

    config = _make_sdk_config(
        primary_model="gpt-4o",
        anthropic_key="",
        openai_key="sk-openai-test-key",
    )
    result = precheck(config=config, state=None)

    assert result.is_ok


def test_precheck_cli_auto_with_codex_state_and_binary() -> None:
    """mode=cli, default_agent="auto", state.init_agent="codex", codex binary found → ok."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="codex")

    with patch("shutil.which", return_value="/usr/local/bin/codex"):
        result = precheck(config=config, state=state)

    assert result.is_ok


def test_precheck_never_raises_on_unexpected_agent_name() -> None:
    """precheck() must not raise — even with an agent name it doesn't recognize."""
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="unknown_agent_xyz")
    try:
        result = precheck(config=config, state=None)
        # Should return a failure, not raise
        assert not result.is_ok
    except Exception as exc:
        pytest.fail(f"precheck() raised unexpectedly: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# DEATH TESTS — F2 Part A: unknown agent names return reason="unknown_agent"
# (not "cli_binary_missing") — iter-F2 scar fix
# ---------------------------------------------------------------------------


def test_death_f2_hyphen_typo_in_state_returns_unknown_agent() -> None:
    """Death test F2: state.init_agent='claude-code' (hyphen typo) with default_agent='auto'
    must return fail(reason='unknown_agent'), NOT ok() and NOT cli_binary_missing.

    Silent failure path: if this returns ok(), dispatch proceeds with a string that
    no dispatcher recognises, and the session is silently mis-routed or crashes at
    subprocess invocation time with a cryptic error.
    """
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="claude-code")  # hyphen typo — not in _AGENT_BINARY_MAP

    result = precheck(config=config, state=state)

    assert not result.is_ok, (
        "precheck() must fail for unknown agent 'claude-code' (hyphen typo). "
        "If it returns ok(), dispatch will silently mis-route the session."
    )
    assert result.reason == "unknown_agent", (
        f"Expected reason='unknown_agent' for unrecognised agent name. "
        f"Got reason={result.reason!r}. "
        f"'cli_binary_missing' is dishonest: we don't know which binary to look for."
    )


def test_death_f2_uppercase_returns_unknown_agent() -> None:
    """Death test F2: state.init_agent='CLAUDE_CODE' (case mismatch) must return
    fail(reason='unknown_agent').

    Silent failure path: if this returns ok() due to a case-insensitive lookup,
    dispatch uses the wrong agent string. If it returns cli_binary_missing, the
    operator misdiagnoses the problem as a missing binary rather than a config typo.
    """
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="CLAUDE_CODE")  # uppercase — not in _AGENT_BINARY_MAP

    result = precheck(config=config, state=state)

    assert not result.is_ok, "precheck() must fail for unknown agent 'CLAUDE_CODE' (case mismatch)."
    assert result.reason == "unknown_agent", (
        f"Expected reason='unknown_agent' for unrecognised agent name. "
        f"Got reason={result.reason!r}."
    )


def test_death_f2_unknown_default_agent_in_config_returns_unknown_agent() -> None:
    """Death test F2: default_agent='gemini_cli' (unknown) must return fail(reason='unknown_agent').

    Silent failure path: if this returns cli_binary_missing, the user sees 'install gemini_cli binary'
    when the real problem is that gemini_cli is not a supported agent at all.
    """
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="gemini_cli")
    # No state needed — default_agent is explicitly set (not "auto")

    result = precheck(config=config, state=None)

    assert not result.is_ok, "precheck() must fail for unsupported agent 'gemini_cli'."
    assert result.reason == "unknown_agent", (
        f"Expected reason='unknown_agent' for completely unrecognised agent. "
        f"Got reason={result.reason!r}."
    )


def test_death_f2_unknown_agent_message_names_valid_agents() -> None:
    """Death test F2: the failure message for unknown_agent must name the valid options.

    Diagnostic quality test: if the message doesn't name valid agents, the operator
    cannot fix the problem without reading source code.
    """
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="my_custom_agent")

    result = precheck(config=config, state=None)

    assert not result.is_ok
    assert result.reason == "unknown_agent"
    # Message must guide the operator — mention at least one valid agent
    assert "claude_code" in result.message or "codex" in result.message, (
        f"Failure message for unknown_agent must list valid agent names. Got: {result.message!r}"
    )


# ---------------------------------------------------------------------------
# DEATH TESTS — F3: state.init_agent="auto" (corrupt state) must NOT raise KeyError
# (iter-F2 round-1 yin-review CRITICAL fix)
# ---------------------------------------------------------------------------


def test_death_f2_state_init_agent_auto_returns_unknown_agent_not_keyerror() -> None:
    """state.init_agent='auto' must fail with unknown_agent, NOT raise KeyError.

    This is corrupted state: the 'auto' sentinel should only appear in
    config-level default_agent, never in state.init_agent. If it does,
    the enum check must reject it cleanly per the 'precheck never raises' contract.

    Silent failure path before this fix:
      1. default_agent="auto" → resolution branch reads state.init_agent
      2. state.init_agent="auto" → effective_agent remains "auto"
      3. "auto" was in _KNOWN_CLI_AGENTS → enum check PASSES
      4. "auto" not in _UNSUPPORTED_CLI_AGENTS → opencode check PASSES
      5. _AGENT_BINARY_MAP["auto"] → KeyError: "auto" is not a binary map key
      6. KeyError propagates → violates "precheck never raises" contract
      7. Server crashes with raw KeyError traceback instead of actionable fail message
    """
    from secondsight.config.precheck import precheck

    config = _make_cli_config(mode="cli", default_agent="auto")
    state = _make_state(agent="auto")  # corrupt: "auto" should never appear in state.init_agent

    # MUST NOT raise KeyError — precheck never raises
    result = precheck(config=config, state=state)

    assert result.is_ok is False, (
        "precheck() must return a failure result for corrupted state.init_agent='auto', "
        "not raise an exception."
    )
    assert result.reason == "unknown_agent", (
        f"Expected reason='unknown_agent' for corrupted state.init_agent='auto'. "
        f"Got reason={result.reason!r}. "
        f"The 'auto' sentinel must be caught at the enum check with a clean failure, "
        f"not cause a KeyError in _AGENT_BINARY_MAP."
    )
    assert "auto" in result.message, (
        f"The failure message must name the problematic value 'auto' so the operator "
        f"knows what was in state.init_agent. Got: {result.message!r}"
    )
