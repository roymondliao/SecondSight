"""Death tests and unit tests for ProjectAnalysisRuntime.dispatch() mode branching.

Death tests MUST come first — each targets a silent failure path.

Death case reference (from task-6.md):
  Mode dispatch: dispatch() must route to CLIAnalysisDispatcher for mode=cli
                 and SDKAnalysisDispatcher for mode=sdk.
  Mode agnosticism: sweeper and analyze.py must NOT reference config.general.mode.
  DB schema: AnalysisOutput fields must be persisted correctly.

The key invariant: mode-awareness lives ONLY in ProjectAnalysisRuntime.dispatch().
No caller references mode directly.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from secondsight.analysis.output import AnalysisOutput
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


def _make_cli_config(default_agent: str = "claude_code") -> SecondSightConfig:
    return SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode="cli"),
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


def _make_sdk_config() -> SecondSightConfig:
    return SecondSightConfig(
        retention=_make_retention(),
        general=GeneralConfig(mode="sdk"),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY="sk-ant-test"),
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


def _make_state(agent: str = "claude_code") -> SecondSightState:
    return SecondSightState(
        schema_version="1.0",
        init_agent=agent,
        init_at="2026-05-14T00:00:00+00:00",
        secondsight_version="0.1.0",
    )


def _make_success_output(dispatched_via: str, agent_or_model: str) -> AnalysisOutput:
    """Helper to build a mock AnalysisOutput for the given mode."""
    if dispatched_via == "cli":
        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": "sess-test",
                "status": "success",
                "behavior_flags": [],
                "session_summary": {
                    "headline": "CLI analysis complete",
                    "key_findings": [],
                    "body": "Mock CLI dispatch.",
                },
                "dispatched_via": "cli",
                "cli_agent": agent_or_model,
                "primary_model": None,
                "fallback_used": False,
                "retry_count": 0,
                "error_details": None,
            }
        )
    else:
        return AnalysisOutput.model_validate(
            {
                "schema_version": "1.0",
                "session_id": "sess-test",
                "status": "success",
                "behavior_flags": [],
                "session_summary": {
                    "headline": "SDK analysis complete",
                    "key_findings": [],
                    "body": "Mock SDK dispatch.",
                },
                "dispatched_via": "sdk",
                "cli_agent": None,
                "primary_model": agent_or_model,
                "fallback_used": False,
                "retry_count": 0,
                "error_details": None,
            }
        )


# ---------------------------------------------------------------------------
# DEATH TEST: Mode-agnosticism — sweeper must NOT reference mode
# ---------------------------------------------------------------------------


def test_sweeper_module_does_not_reference_mode() -> None:
    """Architectural guardrail: sweeper code must NOT reference config.general.mode."""
    # The sweeper lives in sdk/trigger.py (Sweeper class) and server.py (_ServerSweepCoordinator)
    forbidden_patterns = [r"\.mode\b", r"mode\s*==", r"general\.mode", r'"cli"', r'"sdk"']

    sweeper_files = [
        Path("/Users/yuyu_liao/vicone/SecondSight/src/secondsight/sdk/trigger.py"),
        Path("/Users/yuyu_liao/vicone/SecondSight/src/secondsight/api/server.py"),
    ]

    violations: list[str] = []
    for f in sweeper_files:
        text = f.read_text()
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, text)
            if matches:
                # Check if these are mode references vs innocent 'cli' mentions
                # (e.g. 'cli_dispatcher' contains 'cli' but is not a mode check)
                if pattern in (r"\.mode\b", r"mode\s*==", r"general\.mode"):
                    violations.append(f"{f.name}: pattern={pattern!r}, matches={matches!r}")
                elif pattern in (r'"cli"', r'"sdk"'):
                    # Only flag exact "cli" or "sdk" string literals if used in mode comparison
                    # context. Search for the pattern in mode-comparison context specifically.
                    mode_comparison_pattern = rf'(?:mode\s*==\s*{pattern}|{pattern}\s*==\s*mode)'
                    mode_matches = re.findall(mode_comparison_pattern, text)
                    if mode_matches:
                        violations.append(
                            f"{f.name}: mode comparison found: {mode_matches!r}"
                        )

    assert not violations, (
        f"Sweeper module(s) contain forbidden mode references: {violations}. "
        f"Mode-awareness must live ONLY in ProjectAnalysisRuntime.dispatch()."
    )


def test_manual_analyze_cli_does_not_reference_mode() -> None:
    """Architectural guardrail: analyze.py must NOT reference config.general.mode."""
    analyze_file = Path(
        "/Users/yuyu_liao/vicone/SecondSight/src/secondsight/cli/analyze.py"
    )
    text = analyze_file.read_text()

    forbidden_patterns = [r"\.mode\b", r"mode\s*==", r"general\.mode"]

    violations: list[str] = []
    for pattern in forbidden_patterns:
        matches = re.findall(pattern, text)
        if matches:
            violations.append(f"analyze.py: pattern={pattern!r}, matches={matches!r}")

    assert not violations, (
        f"analyze.py contains forbidden mode references: {violations}. "
        f"Mode-awareness must live ONLY in ProjectAnalysisRuntime.dispatch()."
    )


# ---------------------------------------------------------------------------
# DEATH TEST: dispatch() mode routing — CLI dispatcher called for mode=cli
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_mode_cli_calls_cli_dispatcher(tmp_path: Path) -> None:
    """mode=cli + valid config → ProjectAnalysisRuntime.dispatch() calls CLIAnalysisDispatcher."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config(default_agent="claude_code")
    state = _make_state(agent="claude_code")
    session_payload = {"events": []}
    project_root = tmp_path

    mock_cli_output = _make_success_output("cli", "claude_code")

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = AsyncMock(return_value=mock_cli_output)

    # Inject mock via constructor
    mad = ModeAwareDispatch(
        config=config,
        state=state,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    result = await mad.dispatch(
        session_id="sess-test",
        session_payload=session_payload,
        project_root=project_root,
    )

    mock_cli_dispatcher.dispatch.assert_called_once()
    assert result.dispatched_via == "cli"


@pytest.mark.asyncio
async def test_dispatch_mode_sdk_calls_sdk_dispatcher(tmp_path: Path) -> None:
    """mode=sdk + valid config → ProjectAnalysisRuntime.dispatch() calls SDKAnalysisDispatcher."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_sdk_config()
    session_payload = {"events": []}

    mock_sdk_output = _make_success_output("sdk", "claude-haiku-4-5-20251001")

    mock_sdk_dispatcher = MagicMock()
    mock_sdk_dispatcher.dispatch = AsyncMock(return_value=mock_sdk_output)

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        cli_dispatcher=None,
        sdk_dispatcher=mock_sdk_dispatcher,
    )

    result = await mad.dispatch(
        session_id="sess-test",
        session_payload=session_payload,
        project_root=tmp_path,
    )

    mock_sdk_dispatcher.dispatch.assert_called_once()
    assert result.dispatched_via == "sdk"


@pytest.mark.asyncio
async def test_dispatch_cli_mode_dispatched_via_is_cli(tmp_path: Path) -> None:
    """dispatched_via on returned AnalysisOutput must equal 'cli' for mode=cli."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config(default_agent="claude_code")
    state = _make_state(agent="claude_code")

    mock_output = _make_success_output("cli", "claude_code")
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch = AsyncMock(return_value=mock_output)

    mad = ModeAwareDispatch(
        config=config,
        state=state,
        cli_dispatcher=mock_dispatcher,
        sdk_dispatcher=None,
    )
    result = await mad.dispatch("sess-001", {}, project_root=tmp_path)

    assert result.dispatched_via == "cli"


@pytest.mark.asyncio
async def test_dispatch_sdk_mode_dispatched_via_is_sdk(tmp_path: Path) -> None:
    """dispatched_via on returned AnalysisOutput must equal 'sdk' for mode=sdk."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_sdk_config()

    mock_output = _make_success_output("sdk", "claude-haiku-4-5-20251001")
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch = AsyncMock(return_value=mock_output)

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        cli_dispatcher=None,
        sdk_dispatcher=mock_dispatcher,
    )
    result = await mad.dispatch("sess-002", {}, project_root=tmp_path)

    assert result.dispatched_via == "sdk"


# ---------------------------------------------------------------------------
# UNIT TESTS — ModeAwareDispatch construction
# ---------------------------------------------------------------------------


def test_mode_aware_dispatch_importable() -> None:
    """ModeAwareDispatch can be imported from runtime without error."""
    from secondsight.analysis.runtime import ModeAwareDispatch  # noqa: F401


def test_mode_aware_dispatch_has_dispatch_method() -> None:
    """ModeAwareDispatch has an async dispatch() method."""
    import inspect

    from secondsight.analysis.runtime import ModeAwareDispatch

    assert hasattr(ModeAwareDispatch, "dispatch")
    assert inspect.iscoroutinefunction(ModeAwareDispatch.dispatch)


def test_project_analysis_runtime_has_mode_aware_dispatch_attribute() -> None:
    """ProjectAnalysisRuntime has a mode_aware_dispatch attribute after build."""
    # This tests that the runtime factory wires up ModeAwareDispatch.
    # We test via type annotation / attribute existence, not actual construction
    # (which would require full DB setup).
    from secondsight.analysis.runtime import ProjectAnalysisRuntime

    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ProjectAnalysisRuntime)}
    assert "mode_aware_dispatch" in field_names, (
        f"ProjectAnalysisRuntime must have a 'mode_aware_dispatch' field. "
        f"Found fields: {field_names}"
    )
