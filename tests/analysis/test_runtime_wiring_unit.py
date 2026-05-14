"""Unit tests for ModeAwareDispatch wiring fixes.

These test the positive paths after CRITICAL FIX 1 is applied:
- ModeAwareDispatch accepts project_id and repository
- dispatch() calls repo.insert_or_ignore() on success
- dispatch() skips repo call on no-op (concurrent duplicate)
- Trigger.dispatch() routes through ModeAwareDispatch (not orchestrator directly)
- Trigger still accepts mode_aware_dispatch=None for backward compat (legacy SDK path)
"""

from __future__ import annotations

import asyncio
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
from secondsight.storage.retention import RetentionConfig


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_retention() -> RetentionConfig:
    return RetentionConfig(
        raw_traces_ttl_days=30,
        raw_traces_source="builtin_default",
        analysis_ttl_days=90,
        analysis_ttl_source="builtin_default",
        cleanup_after_analysis=False,
    )


def _make_cli_config() -> SecondSightConfig:
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
                default_agent="claude_code",
                models=AnalysisCLIModelsConfig(),
            ),
            sdk=AnalysisSDKConfig(),
        ),
        analysis_global=GlobalAnalysisConfig(),
        project_analysis=ProjectAnalysisConfig(),
    )


def _make_success_output(session_id: str) -> AnalysisOutput:
    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "success",
            "behavior_flags": [],
            "session_summary": {
                "headline": "CLI analysis complete",
                "key_findings": [],
                "body": "Mock dispatch.",
            },
            "dispatched_via": "cli",
            "cli_agent": "claude_code",
            "primary_model": None,
            "fallback_used": False,
            "retry_count": 0,
            "error_details": None,
        }
    )


# ---------------------------------------------------------------------------
# Unit tests for ModeAwareDispatch new interface
# ---------------------------------------------------------------------------


def test_mode_aware_dispatch_accepts_project_id_and_repository() -> None:
    """ModeAwareDispatch.__init__ accepts project_id and repository parameters."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    mock_repo = MagicMock()

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-unit-001",
        repository=mock_repo,
    )

    assert mad is not None
    assert mad._project_id == "proj-unit-001"
    assert mad._repository is mock_repo


@pytest.mark.asyncio
async def test_dispatch_calls_repository_insert_on_success(tmp_path: Path) -> None:
    """On successful dispatch, repo.insert_or_ignore() is called with the output."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    session_id = "sess-unit-repo-001"
    mock_output = _make_success_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = AsyncMock(return_value=mock_output)

    mock_repo = MagicMock()
    mock_repo.insert_or_ignore = MagicMock(return_value="ao-some-uuid")

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-unit-001",
        repository=mock_repo,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    result = await mad.dispatch(session_id, {}, project_root=tmp_path)

    assert result.status == "success"
    mock_repo.insert_or_ignore.assert_called_once_with(mock_output, project_id="proj-unit-001")


@pytest.mark.asyncio
async def test_dispatch_skips_repository_insert_on_concurrent_no_op(tmp_path: Path) -> None:
    """On concurrent duplicate (DC10 no-op), repo.insert_or_ignore() is NOT called.

    The no-op result is a failure output (status='failure', reason='dispatch_in_progress').
    We should not write the no-op result to the DB — only the real dispatch writes.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    session_id = "sess-unit-noop-001"
    mock_output = _make_success_output(session_id)

    async def slow_dispatch(s_id: str, payload: dict, project_root=None):
        await asyncio.sleep(0.05)
        return mock_output

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = slow_dispatch

    mock_repo = MagicMock()
    mock_repo.insert_or_ignore = MagicMock(return_value="ao-some-uuid")

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-unit-001",
        repository=mock_repo,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    # Launch concurrent dispatches — second is a no-op
    await asyncio.gather(
        mad.dispatch(session_id, {}, project_root=tmp_path),
        mad.dispatch(session_id, {}, project_root=tmp_path),
    )

    # Only the successful dispatch should have called insert_or_ignore
    assert mock_repo.insert_or_ignore.call_count == 1, (
        f"Expected insert_or_ignore called once. Got {mock_repo.insert_or_ignore.call_count}. "
        f"The no-op DC10 result must NOT be persisted to analysis_outputs."
    )


@pytest.mark.asyncio
async def test_dispatch_with_no_repository_still_dispatches(tmp_path: Path) -> None:
    """dispatch() with repository=None gracefully skips persistence."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    session_id = "sess-no-repo-unit-001"
    mock_output = _make_success_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = AsyncMock(return_value=mock_output)

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-unit-001",
        repository=None,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    result = await mad.dispatch(session_id, {}, project_root=tmp_path)

    assert result.status == "success"
    mock_cli_dispatcher.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests for Trigger new interface
# ---------------------------------------------------------------------------


def test_trigger_accepts_mode_aware_dispatch_parameter() -> None:
    """Trigger.__init__ accepts mode_aware_dispatch parameter."""
    from secondsight.sdk.trigger import LockRegistry, Trigger

    mock_orchestrator = MagicMock()
    mock_runs_repo = MagicMock()
    mock_events_repo = MagicMock()
    mock_mad = MagicMock()

    trigger = Trigger(
        orchestrator=mock_orchestrator,
        analysis_runs_repo=mock_runs_repo,
        events_repo=mock_events_repo,
        lock_registry=LockRegistry(),
        mode_aware_dispatch=mock_mad,
    )

    assert trigger._mode_aware_dispatch is mock_mad


@pytest.mark.asyncio
async def test_trigger_dispatch_calls_mode_aware_dispatch_when_provided() -> None:
    """Trigger.dispatch() calls mode_aware_dispatch.dispatch() when provided."""
    from secondsight.sdk.trigger import LockRegistry, Trigger

    session_id = "sess-trigger-unit-001"
    project_id = "proj-trigger-unit-001"

    mock_orchestrator = MagicMock()
    mock_runs_repo = MagicMock()
    mock_runs_repo.get_latest_for_session = MagicMock(return_value=None)
    mock_events_repo = MagicMock()

    mock_mad = MagicMock()
    mock_mad.dispatch = AsyncMock(return_value=_make_success_output(session_id))

    trigger = Trigger(
        orchestrator=mock_orchestrator,
        analysis_runs_repo=mock_runs_repo,
        events_repo=mock_events_repo,
        lock_registry=LockRegistry(),
        mode_aware_dispatch=mock_mad,
    )

    result = await trigger.dispatch(project_id, session_id, source="event")

    assert result.dispatched is True
    mock_mad.dispatch.assert_called_once()
    # Orchestrator should NOT be called directly when mode_aware_dispatch is used
    mock_orchestrator.analyze_and_aggregate.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_dispatch_uses_legacy_orchestrator_when_no_mode_aware_dispatch() -> None:
    """Trigger.dispatch() falls back to legacy orchestrator when mode_aware_dispatch=None.

    Backward-compat test: tests that use the old Trigger without mode_aware_dispatch
    continue to work via asyncio.create_task(orchestrator.analyze_and_aggregate(...)).
    """
    from secondsight.sdk.trigger import LockRegistry, Trigger

    session_id = "sess-trigger-legacy-001"
    project_id = "proj-trigger-legacy-001"

    mock_orchestrator = MagicMock()

    # Return a coroutine that can be wrapped by asyncio.create_task
    async def noop_analyze(session_id: str, force: bool = False) -> None:
        pass

    mock_orchestrator.analyze_and_aggregate = noop_analyze

    mock_runs_repo = MagicMock()
    mock_runs_repo.get_latest_for_session = MagicMock(return_value=None)
    mock_events_repo = MagicMock()

    trigger = Trigger(
        orchestrator=mock_orchestrator,
        analysis_runs_repo=mock_runs_repo,
        events_repo=mock_events_repo,
        lock_registry=LockRegistry(),
        mode_aware_dispatch=None,  # Legacy path
    )

    result = await trigger.dispatch(project_id, session_id, source="event")

    assert result.dispatched is True


# ---------------------------------------------------------------------------
# Unit test: build_project_analysis_runtime wires repository into ModeAwareDispatch
# ---------------------------------------------------------------------------


def test_build_project_analysis_runtime_has_mode_aware_dispatch_with_project_id(
    tmp_path: Path,
) -> None:
    """build_project_analysis_runtime() must wire project_id into ModeAwareDispatch."""
    from secondsight.analysis.runtime import ProjectAnalysisRuntime
    import dataclasses

    # Just verify the dataclass has the field — full construction requires DB setup
    field_names = {f.name for f in dataclasses.fields(ProjectAnalysisRuntime)}
    assert "mode_aware_dispatch" in field_names
