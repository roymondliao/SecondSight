"""DC10 death tests for concurrent dispatch deduplication.

Death case DC10: two ProjectAnalysisRuntime.dispatch() (via ModeAwareDispatch) calls
for the same session_id in parallel → only ONE dispatch actually executes;
the second call returns immediately (no-op or returns same result).

This is an asyncio-level test: uses asyncio.gather to simulate concurrent access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

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


def _make_state(agent: str = "claude_code") -> SecondSightState:
    return SecondSightState(
        schema_version="1.0",
        init_agent=agent,
        init_at="2026-05-14T00:00:00+00:00",
        secondsight_version="0.1.0",
    )


def _make_cli_output(session_id: str) -> AnalysisOutput:
    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "success",
            "behavior_flags": [],
            "session_summary": {
                "headline": "CLI analysis complete",
                "key_findings": [],
                "body": "Mock CLI dispatch.",
            },
            "dispatched_via": "cli",
            "cli_agent": "claude_code",
            "primary_model": None,
            "fallback_used": False,
            "retry_count": 0,
            "error_details": None,
        }
    )


@pytest.mark.asyncio
async def test_dc10_concurrent_dispatch_same_session_id_only_one_executes(
    tmp_path: Path,
) -> None:
    """DC10: two concurrent dispatch() calls for the same session_id → exactly ONE executes.

    The per-session asyncio.Lock in ModeAwareDispatch ensures the second concurrent
    call for the same session_id does not trigger a second LLM dispatch.

    Production contract verification (CRITICAL FIX 2):
    - In-memory dispatch counter == 1 (lock works)
    - analysis_outputs table has exactly 1 row for session_id (persistence contract)
    """
    from secondsight.analysis.runtime import ModeAwareDispatch
    from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository
    from secondsight.storage.db_engine import DBEngine
    import sqlalchemy as sa
    from secondsight.storage.analysis_outputs_table import analysis_outputs

    # Use real DB to verify the production persistence contract
    db_engine = DBEngine(db_path=tmp_path / "intelligence.db")
    repo = AnalysisOutputsRepository(db_engine)
    repo.create_schema()

    config = _make_cli_config()
    state = _make_state(agent="claude_code")
    session_id = "sess-concurrent-001"
    session_payload = {"events": []}
    project_root = tmp_path

    dispatch_call_count = 0

    async def slow_dispatch(session_id: str, session_payload: dict, project_root=None):
        nonlocal dispatch_call_count
        dispatch_call_count += 1
        # Simulate work
        await asyncio.sleep(0.05)
        return _make_cli_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = slow_dispatch

    mad = ModeAwareDispatch(
        config=config,
        state=state,
        project_id="proj-concurrent-001",
        repository=repo,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    # Launch two concurrent dispatches for the SAME session_id
    results = await asyncio.gather(
        mad.dispatch(session_id, session_payload, project_root=project_root),
        mad.dispatch(session_id, session_payload, project_root=project_root),
    )

    # In-memory contract: EXACTLY ONE dispatch should have executed
    assert dispatch_call_count == 1, (
        f"Expected exactly 1 dispatcher call for concurrent same-session dispatch. "
        f"Got {dispatch_call_count} calls. DC10 deduplication is not working."
    )

    # Both results should be AnalysisOutput (the second may be a cached or no-op result)
    assert all(isinstance(r, AnalysisOutput) for r in results)

    # Production contract: exactly ONE row in analysis_outputs (CRITICAL FIX 2)
    with db_engine.engine.connect() as conn:
        row_count = conn.execute(
            sa.select(sa.func.count())
            .select_from(analysis_outputs)
            .where(analysis_outputs.c.session_id == session_id)
        ).scalar()

    assert row_count == 1, (
        f"Expected exactly 1 row in analysis_outputs for session_id={session_id!r}. "
        f"Got {row_count} rows. "
        f"DC10 production contract: only ONE DB row should exist for concurrent dispatches."
    )


@pytest.mark.asyncio
async def test_dc10_different_session_ids_both_execute(tmp_path: Path) -> None:
    """DC10: two concurrent dispatch() calls for DIFFERENT session_ids → both execute."""
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    state = _make_state(agent="claude_code")
    session_payload = {"events": []}
    project_root = tmp_path

    dispatch_call_count = 0

    async def slow_dispatch(session_id: str, session_payload: dict, project_root=None):
        nonlocal dispatch_call_count
        dispatch_call_count += 1
        await asyncio.sleep(0.05)
        return _make_cli_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = slow_dispatch

    mad = ModeAwareDispatch(
        config=config,
        state=state,
        project_id="proj-dc10-different",
        repository=None,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    # Launch two concurrent dispatches for DIFFERENT session_ids
    await asyncio.gather(
        mad.dispatch("sess-A", session_payload, project_root=project_root),
        mad.dispatch("sess-B", session_payload, project_root=project_root),
    )

    # Both dispatches should execute (different session_ids are independent)
    assert dispatch_call_count == 2, (
        f"Expected 2 dispatcher calls for concurrent different-session dispatch. "
        f"Got {dispatch_call_count} calls."
    )


@pytest.mark.asyncio
async def test_dc10_sequential_dispatches_for_same_session_both_execute(
    tmp_path: Path,
) -> None:
    """DC10: sequential (not concurrent) dispatches for same session_id both execute.

    The lock should only block CONCURRENT access. Sequential calls are independent.
    After the first dispatch completes, the lock is released, so the second call
    should proceed normally.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    state = _make_state(agent="claude_code")
    session_id = "sess-sequential-001"
    session_payload = {"events": []}
    project_root = tmp_path

    dispatch_call_count = 0

    async def mock_dispatch(session_id: str, session_payload: dict, project_root=None):
        nonlocal dispatch_call_count
        dispatch_call_count += 1
        return _make_cli_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = mock_dispatch

    mad = ModeAwareDispatch(
        config=config,
        state=state,
        project_id="proj-dc10-sequential",
        repository=None,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    # Sequential calls — NOT concurrent
    await mad.dispatch(session_id, session_payload, project_root=project_root)
    await mad.dispatch(session_id, session_payload, project_root=project_root)

    # Sequential calls should both execute (lock released after each)
    assert dispatch_call_count == 2, (
        f"Expected 2 dispatcher calls for sequential same-session dispatch. "
        f"Got {dispatch_call_count} calls. Sequential dispatches should not be blocked."
    )


@pytest.mark.asyncio
async def test_sequential_rerun_updates_existing_analysis_output_row(tmp_path: Path) -> None:
    """Sequential rerun keeps one DB row and updates it to the latest result."""
    from secondsight.analysis.runtime import ModeAwareDispatch
    from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository
    from secondsight.storage.analysis_outputs_table import analysis_outputs
    from secondsight.storage.db_engine import DBEngine
    import sqlalchemy as sa

    db_engine = DBEngine(db_path=tmp_path / "intelligence.db")
    repo = AnalysisOutputsRepository(db_engine)
    repo.create_schema()

    config = _make_cli_config()
    state = _make_state(agent="claude_code")
    session_id = "sess-rerun-update-001"
    session_payload = {"events": []}
    project_root = tmp_path

    dispatch_count = 0

    async def rerun_dispatch(session_id: str, session_payload: dict, project_root=None):
        nonlocal dispatch_count
        dispatch_count += 1
        output = _make_cli_output(session_id)
        if dispatch_count == 2:
            return AnalysisOutput.model_validate(
                {
                    **output.model_dump(),
                    "status": "failure",
                    "retry_count": 2,
                    "error_details": {"reason": "rerun-overwrite"},
                }
            )
        return output

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = rerun_dispatch

    mad = ModeAwareDispatch(
        config=config,
        state=state,
        project_id="proj-rerun-update",
        repository=repo,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    await mad.dispatch(session_id, session_payload, project_root=project_root)
    first_row = repo.get_by_session_id(session_id)
    assert first_row is not None

    await mad.dispatch(session_id, session_payload, project_root=project_root)
    second_row = repo.get_by_session_id(session_id)
    assert second_row is not None

    with db_engine.engine.connect() as conn:
        row_count = conn.execute(
            sa.select(sa.func.count())
            .select_from(analysis_outputs)
            .where(analysis_outputs.c.session_id == session_id)
        ).scalar()

    assert dispatch_count == 2
    assert row_count == 1
    assert second_row["id"] != first_row["id"]
    assert second_row["status"] == "failure"
    assert second_row["retry_count"] == 2
    assert second_row["error_details"] == {"reason": "rerun-overwrite"}
