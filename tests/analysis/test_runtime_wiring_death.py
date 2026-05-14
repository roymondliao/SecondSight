"""Death tests for ModeAwareDispatch wiring into the production call graph.

These tests verify the CRITICAL FIX 1 requirement: that ModeAwareDispatch is
actually called by production code (Trigger), not just constructed and stored.

Death case: if ModeAwareDispatch is deleted, these tests must FAIL.
Death case: if AnalysisOutputsRepository is not called after dispatch, these tests must FAIL.

Test execution order follows samsara contract: death tests run before unit tests.
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
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.retention import RetentionConfig


# ---------------------------------------------------------------------------
# Shared helpers (duplicated from other test files — will be resolved by
# IMPORTANT FIX 9 which moves these to conftest.py)
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


# ---------------------------------------------------------------------------
# DEATH TEST 1: ModeAwareDispatch.dispatch() persists output to repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dt_dispatch_persists_output_to_repository(tmp_path: Path) -> None:
    """DEATH TEST: After ModeAwareDispatch.dispatch() succeeds, a row exists in analysis_outputs.

    This is the production contract: dispatch() must call
    repo.insert_or_ignore(output, project_id=...) after the dispatcher returns.

    If this test fails with "row is None", it means ModeAwareDispatch.dispatch()
    does not call the repository — the wiring is missing.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch
    from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository

    db_path = tmp_path / "intelligence.db"
    db_engine = DBEngine(db_path=db_path)

    repo = AnalysisOutputsRepository(db_engine)
    repo.create_schema()

    config = _make_cli_config()
    session_id = "sess-wiring-death-001"
    mock_output = _make_success_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = AsyncMock(return_value=mock_output)

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-death-001",
        repository=repo,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    result = await mad.dispatch(session_id, {}, project_root=tmp_path)

    assert result.status == "success"

    # THE PRODUCTION CONTRACT: a row must exist in analysis_outputs after dispatch
    row = repo.get_by_session_id(session_id)
    assert row is not None, (
        f"No row in analysis_outputs for session_id={session_id!r}. "
        f"ModeAwareDispatch.dispatch() must call repo.insert_or_ignore() after dispatching. "
        f"WIRING IS MISSING."
    )
    assert row["project_id"] == "proj-death-001"
    assert row["dispatched_via"] == "cli"
    assert row["status"] == "success"


# ---------------------------------------------------------------------------
# DEATH TEST 2: DC10 verified via DB rows (not just in-memory counter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dt_dc10_concurrent_dispatch_produces_exactly_one_db_row(
    tmp_path: Path,
) -> None:
    """DEATH TEST DC10: two concurrent dispatch() calls → exactly ONE row in analysis_outputs.

    The production contract is NOT just "in-memory dispatch counter == 1".
    The production contract is "analysis_outputs table has exactly ONE row for session_id".

    If this test fails with row_count == 2, the DB UNIQUE constraint or INSERT OR IGNORE
    are not working. If it fails with row_count == 0, dispatch() is not calling
    repo.insert_or_ignore() at all.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch
    from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository

    db_path = tmp_path / "intelligence.db"
    db_engine = DBEngine(db_path=db_path)

    repo = AnalysisOutputsRepository(db_engine)
    repo.create_schema()

    config = _make_cli_config()
    session_id = "sess-dc10-db-001"
    mock_output = _make_success_output(session_id)

    async def slow_dispatch(s_id: str, payload: dict, project_root=None):
        await asyncio.sleep(0.05)
        return mock_output

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = slow_dispatch

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-dc10-001",
        repository=repo,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    # Launch two concurrent dispatches for the SAME session_id
    await asyncio.gather(
        mad.dispatch(session_id, {}, project_root=tmp_path),
        mad.dispatch(session_id, {}, project_root=tmp_path),
    )

    # THE PRODUCTION CONTRACT: exactly ONE row in DB
    import sqlalchemy as sa
    from secondsight.storage.analysis_outputs_table import analysis_outputs

    with db_engine.engine.connect() as conn:
        count_result = conn.execute(
            sa.select(sa.func.count())
            .select_from(analysis_outputs)
            .where(analysis_outputs.c.session_id == session_id)
        ).scalar()

    assert count_result == 1, (
        f"Expected exactly 1 row in analysis_outputs for session_id={session_id!r}. "
        f"Got {count_result} rows. "
        f"DC10 deduplication must produce exactly one DB row for concurrent dispatches."
    )


# ---------------------------------------------------------------------------
# DEATH TEST 3: Trigger.dispatch() routes through ModeAwareDispatch
# (Not directly to orchestrator.analyze_and_aggregate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dt_trigger_dispatch_calls_mode_aware_dispatch(tmp_path: Path) -> None:
    """DEATH TEST: Trigger.dispatch() must route through ModeAwareDispatch.dispatch().

    The Trigger currently calls orchestrator.analyze_and_aggregate() directly
    (the legacy SDK-only path). After the fix, it must call
    mode_aware_dispatch.dispatch() instead.

    If this test fails with "mode_aware_dispatch was NOT called",
    the Trigger still uses the legacy path and ModeAwareDispatch is dead code.
    """
    from secondsight.sdk.trigger import LockRegistry, Trigger

    session_id = "sess-trigger-routing-001"
    project_id = "proj-trigger-001"

    # Mock the orchestrator (should NOT be called directly after fix)
    mock_orchestrator = MagicMock()
    mock_orchestrator.analyze_and_aggregate = AsyncMock()

    # Mock analysis_runs_repo — no terminal run exists
    mock_runs_repo = MagicMock()
    mock_runs_repo.get_latest_for_session = MagicMock(return_value=None)

    # Mock events_repo
    mock_events_repo = MagicMock()

    # Mock ModeAwareDispatch
    mock_mad = MagicMock()
    mock_mad.dispatch = AsyncMock(return_value=_make_success_output(session_id))

    trigger = Trigger(
        orchestrator=mock_orchestrator,
        analysis_runs_repo=mock_runs_repo,
        events_repo=mock_events_repo,
        lock_registry=LockRegistry(),
        mode_aware_dispatch=mock_mad,
    )

    result = await trigger.dispatch(
        project_id,
        session_id,
        source="event",
    )

    assert result.dispatched is True, (
        f"Trigger.dispatch() returned dispatched=False: reason={result.reason!r}. "
        f"Expected dispatched=True."
    )

    # THE CRITICAL CHECK: ModeAwareDispatch.dispatch() must have been called
    (
        mock_mad.dispatch.assert_called_once_with(
            session_id,
            project_id=project_id,
        ),
        (
            "Trigger.dispatch() did NOT call ModeAwareDispatch.dispatch(). "
            "The production routing is missing — ModeAwareDispatch is dead code."
        ),
    )


# ---------------------------------------------------------------------------
# DEATH TEST 4: ModeAwareDispatch accepts project_id at init
# ---------------------------------------------------------------------------


def test_dt_mode_aware_dispatch_accepts_project_id() -> None:
    """DEATH TEST: ModeAwareDispatch.__init__ must accept project_id parameter.

    This is required so dispatch() can call repo.insert_or_ignore(output, project_id=...).
    If ModeAwareDispatch does not accept project_id, it cannot persist the output.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()

    # This must NOT raise TypeError about unexpected keyword argument
    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-test-001",
        repository=None,  # repository can be None (no persistence in tests)
    )

    assert mad is not None


# ---------------------------------------------------------------------------
# DEATH TEST 5: ModeAwareDispatch with repository=None does not crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dt_mode_aware_dispatch_without_repository_still_dispatches(
    tmp_path: Path,
) -> None:
    """DEATH TEST: ModeAwareDispatch with repository=None must still dispatch (graceful degradation).

    In test contexts or during migration, the repository may be None.
    dispatch() must still call the underlying dispatcher and return the output.
    It should log a warning about missing persistence but not raise.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch

    config = _make_cli_config()
    session_id = "sess-no-repo-001"
    mock_output = _make_success_output(session_id)

    mock_cli_dispatcher = MagicMock()
    mock_cli_dispatcher.dispatch = AsyncMock(return_value=mock_output)

    mad = ModeAwareDispatch(
        config=config,
        state=None,
        project_id="proj-no-repo",
        repository=None,
        cli_dispatcher=mock_cli_dispatcher,
        sdk_dispatcher=None,
    )

    # Must not raise even without repository
    result = await mad.dispatch(session_id, {}, project_root=tmp_path)

    assert result.status == "success"
    mock_cli_dispatcher.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# DEATH TESTS for CRITICAL FIX 1: invariant — build_project_analysis_runtime
# must NOT call _build_analysis_agent (mode-conditional outside ModeAwareDispatch)
# ---------------------------------------------------------------------------


def test_dt_build_runtime_cli_mode_never_calls_build_analysis_agent(
    tmp_path: Path,
) -> None:
    """DEATH TEST: build_project_analysis_runtime with CLI config must NOT call
    _build_analysis_agent at any point.

    Architecture invariant (runtime.py lines 8-21): only ModeAwareDispatch may
    reference config.general.mode. If build_project_analysis_runtime calls
    _build_analysis_agent for CLI mode, it branches on mode outside
    ModeAwareDispatch — violating the invariant and silently failing for any
    CLI-mode user without provider API keys (RouterTerminalError).

    Silent failure path: if this test is absent and the invariant is violated,
    CLI mode silently fails at runtime with RouterTerminalError, only caught by
    E2E tests with SECONDSIGHT_TEST_REAL_CLI=1.
    """
    from unittest.mock import patch

    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.events_repository import EventsRepository
    from secondsight.storage.raw_trace_store import RawTraceStore

    # Minimal CLI-mode config.toml
    ss_home = tmp_path / ".secondsight"
    ss_home.mkdir(parents=True, exist_ok=True)
    project_dir = ss_home / "projects" / "proj-invariant-cli"
    project_dir.mkdir(parents=True, exist_ok=True)
    (ss_home / "config.toml").write_text(
        '[general]\nmode = "cli"\n[analysis.cli]\ndefault_agent = "claude_code"\n',
        encoding="utf-8",
    )

    db_path = project_dir / "intelligence.db"
    db_engine = DBEngine(db_path=db_path)
    events_repo = EventsRepository(db_engine)
    raw_trace_store = RawTraceStore(project_dir)

    with patch(
        "secondsight.analysis.runtime._build_analysis_agent"
    ) as mock_build_agent:
        from secondsight.analysis import runtime as _runtime_module

        _runtime_module.build_project_analysis_runtime(
            secondsight_home=ss_home,
            project_id="proj-invariant-cli",
            db_engine=db_engine,
            events_repository=events_repo,
            raw_trace_store=raw_trace_store,
        )

    assert mock_build_agent.call_count == 0, (
        f"build_project_analysis_runtime called _build_analysis_agent "
        f"{mock_build_agent.call_count} time(s) for CLI mode. "
        f"Expected 0 calls. "
        f"_build_analysis_agent must only be called inside "
        f"ModeAwareDispatch._get_sdk_dispatcher() (lazy, on first SDK dispatch). "
        f"This invariant prevents RouterTerminalError for CLI-mode users."
    )


def test_dt_build_runtime_sdk_mode_never_calls_build_analysis_agent(
    tmp_path: Path,
) -> None:
    """DEATH TEST: build_project_analysis_runtime with SDK config must NOT call
    _build_analysis_agent eagerly.

    After CRITICAL FIX 1, _build_analysis_agent is lazy: it is only called
    inside ModeAwareDispatch._get_sdk_dispatcher() on the first SDK dispatch,
    not at runtime construction time.

    This test verifies that build_project_analysis_runtime does NOT call
    _build_analysis_agent even for SDK mode.
    """
    from unittest.mock import patch

    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.events_repository import EventsRepository
    from secondsight.storage.raw_trace_store import RawTraceStore

    ss_home = tmp_path / ".secondsight"
    ss_home.mkdir(parents=True, exist_ok=True)
    project_dir = ss_home / "projects" / "proj-invariant-sdk"
    project_dir.mkdir(parents=True, exist_ok=True)
    (ss_home / "config.toml").write_text(
        '[general]\nmode = "sdk"\n'
        "[analysis.sdk]\n"
        'primary_model = "claude-haiku-4-5-20251001"\n'
        'fallback_model = "gpt-4o-mini"\n'
        "[providers.anthropic]\n"
        'ANTHROPIC_API_KEY = "sk-test-sdk-invariant"\n',
        encoding="utf-8",
    )

    db_path = project_dir / "intelligence.db"
    db_engine = DBEngine(db_path=db_path)
    events_repo = EventsRepository(db_engine)
    raw_trace_store = RawTraceStore(project_dir)

    with patch(
        "secondsight.analysis.runtime._build_analysis_agent"
    ) as mock_build_agent:
        from secondsight.analysis import runtime as _runtime_module

        _runtime_module.build_project_analysis_runtime(
            secondsight_home=ss_home,
            project_id="proj-invariant-sdk",
            db_engine=db_engine,
            events_repository=events_repo,
            raw_trace_store=raw_trace_store,
        )

    assert mock_build_agent.call_count == 0, (
        f"build_project_analysis_runtime called _build_analysis_agent "
        f"{mock_build_agent.call_count} time(s) for SDK mode at construction. "
        f"Expected 0 calls. "
        f"_build_analysis_agent must be lazy (inside _get_sdk_dispatcher), "
        f"not called eagerly at build_project_analysis_runtime time."
    )


def test_dt_mode_aware_dispatch_get_sdk_dispatcher_calls_build_analysis_agent(
    tmp_path: Path,
) -> None:
    """DEATH TEST: ModeAwareDispatch._get_sdk_dispatcher() must call
    _build_analysis_agent (or construct SDKAnalysisDispatcher) when called.

    After CRITICAL FIX 1, agent construction happens lazily inside
    _get_sdk_dispatcher(). This test verifies the lazy path is actually
    wired — if it returns a no-op or None, SDK dispatch silently breaks.

    We test via SDKAnalysisDispatcher instantiation (which requires LLMRouter
    with a real key) — verifying that _get_sdk_dispatcher() constructs a
    working dispatcher when an SDK config is provided.
    """
    from secondsight.analysis.runtime import ModeAwareDispatch
    from secondsight.config.schema import (
        AnalysisCLIConfig,
        AnalysisConfig,
        AnalysisSDKConfig,
        GeneralConfig,
        GlobalAnalysisConfig,
        ProjectAnalysisConfig,
        ProviderAnthropicConfig,
        ProviderCustomConfig,
        ProviderOpenAIConfig,
        ProvidersConfig,
    )
    from secondsight.storage.retention import RetentionConfig

    sdk_cfg = SecondSightConfig(
        retention=RetentionConfig(
            raw_traces_ttl_days=30,
            raw_traces_source="builtin_default",
            analysis_ttl_days=90,
            analysis_ttl_source="builtin_default",
            cleanup_after_analysis=False,
        ),
        general=GeneralConfig(mode="sdk"),
        providers=ProvidersConfig(
            anthropic=ProviderAnthropicConfig(ANTHROPIC_API_KEY="sk-test-invariant-lazy"),
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

    mad = ModeAwareDispatch(
        config=sdk_cfg,
        state=None,
        project_id="proj-lazy-sdk",
        repository=None,
        cli_dispatcher=None,
        sdk_dispatcher=None,  # no injection — must construct lazily
    )

    # _get_sdk_dispatcher() must return a real SDKAnalysisDispatcher (not None, not crash)
    dispatcher = mad._get_sdk_dispatcher()

    from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

    assert isinstance(dispatcher, SDKAnalysisDispatcher), (
        f"ModeAwareDispatch._get_sdk_dispatcher() must return SDKAnalysisDispatcher. "
        f"Got: {type(dispatcher).__name__}. "
        f"If this is None or a mock, SDK dispatch is silently broken."
    )
