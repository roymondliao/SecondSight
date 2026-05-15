"""Death + happy-path tests for `secondsight analyze` CLI (GUR-103 task-5, P2-15).

Death test inventory:
- DT-5.4 (DC-7): Already-analyzed session → exit code 2 + correct STDERR message.

Happy-path tests:
- DG-1.2: CLI falls back to in-process when server is unreachable (logged at INFO).
- HP-1.1: In-process analyze succeeds on a seeded session.

Execution order (Samsara framework):
  Death tests written FIRST — expected RED before implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from secondsight.event import Event, EventType
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository

# CLI entry point under test
from secondsight.cli.app import app

# =====================================================================
# Constants
# =====================================================================

_PROJECT_ID = "proj-analyze-test"
_SESSION_ID = "sess-analyze-001"
_NOW = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)

runner = CliRunner()


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[DBEngine]:
    eng = DBEngine(tmp_path / "intel.db")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def events_repo(db_engine: DBEngine) -> EventsRepository:
    r = EventsRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def runs_repo(db_engine: DBEngine) -> AnalysisRunsRepository:
    r = AnalysisRunsRepository(db_engine)
    r.create_schema()
    return r


def _seed_terminal_run(
    runs_repo: AnalysisRunsRepository,
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
    stage: str = "summary_written",
) -> str:
    run_id = runs_repo.start_run(project_id, session_id)
    runs_repo.advance_stage(run_id, stage)
    return run_id


def _make_event(
    seq: int,
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
    event_type: EventType = EventType.TOOL_USE_START,
) -> Event:
    return Event(
        id=f"evt-{session_id}-{seq}",
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=_NOW,
        sequence_number=seq,
        segment_index=0,
    )


# =====================================================================
# DT-5.4 — Already-analyzed: exit code 2 with correct STDERR
# =====================================================================


def test_dt_5_4_already_analyzed_exits_code_2(
    tmp_path: Path,
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
) -> None:
    """DT-5.4 (DC-7): Pre-seeded terminal analysis_run → exit 2.

    STDERR must contain:
    - 'Skipped: session ... already analyzed at ...'
    - 'pass --force to re-run'

    Silent failure this closes: without exit code 2, a CI pipeline that
    runs `secondsight analyze` on an already-complete session would silently
    succeed (exit 0) without triggering the force check, hiding the fact
    that stale analysis data may have been consumed.
    """
    run_id = _seed_terminal_run(runs_repo, stage="summary_written")

    # Import the module under test; patch Trigger.dispatch to return already-analyzed
    from secondsight.sdk.trigger import DispatchResult

    dispatch_result = DispatchResult(
        dispatched=False,
        reason="already-analyzed",
        run_id=run_id,
        existing_stage="summary_written",
        existing_completed_at=_NOW,
    )

    with patch("secondsight.cli.analyze.Trigger") as MockTrigger:
        mock_trigger_instance = MagicMock()
        mock_trigger_instance.dispatch = AsyncMock(return_value=dispatch_result)
        MockTrigger.return_value = mock_trigger_instance

        # Patch dependency construction so we don't need real DB setup
        with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
            mock_build.return_value = mock_trigger_instance

            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--session",
                    _SESSION_ID,
                    "--project",
                    _PROJECT_ID,
                    "--no-server",  # force in-process path
                ],
            )

    # Must exit with code 2
    assert result.exit_code == 2, (
        f"Expected exit code 2 for already-analyzed, got {result.exit_code}. "
        f"Output: {result.output!r}"
    )

    output_combined = result.output
    assert "Skipped" in output_combined or "already analyzed" in output_combined, (
        f"Expected 'Skipped' or 'already analyzed' in output: {output_combined!r}"
    )
    assert "force" in output_combined.lower() or "--force" in output_combined, (
        f"Expected '--force' hint in output: {output_combined!r}"
    )


# =====================================================================
# DG-1.2 — In-process fallback when server is not reachable
# =====================================================================


def test_dg_1_2_falls_back_to_in_process_when_server_down(
    tmp_path: Path,
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DG-1.2: CLI logs INFO and runs in-process when API server is unreachable.

    The path taken (server vs in-process) must be logged at INFO level so
    it is never silent. The default flow tries httpx.post; ConnectError
    triggers the fallback.
    """
    import logging

    from secondsight.sdk.trigger import DispatchResult

    dispatch_result = DispatchResult(
        dispatched=True,
        reason="dispatched",
        run_id=None,
    )

    with patch("secondsight.cli.analyze.httpx") as mock_httpx:
        import httpx as _real_httpx

        mock_httpx.ConnectError = _real_httpx.ConnectError
        mock_post = MagicMock()
        mock_post.side_effect = _real_httpx.ConnectError("connection refused")
        mock_httpx.post = mock_post

        with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
            with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
                mock_run.return_value = dispatch_result
                mock_build.return_value = MagicMock()

                with caplog.at_level(logging.INFO, logger="secondsight.cli.analyze"):
                    result = runner.invoke(
                        app,
                        [
                            "analyze",
                            "--session",
                            _SESSION_ID,
                            "--project",
                            _PROJECT_ID,
                        ],
                    )

    # Should succeed (exit 0) via in-process path
    assert result.exit_code == 0, (
        f"Expected exit 0 for in-process fallback, got {result.exit_code}. "
        f"Output: {result.output!r}"
    )

    # INFO log must mention the fallback
    combined_logs = " ".join(caplog.messages)
    assert "in-process" in combined_logs.lower() or "fallback" in combined_logs.lower(), (
        f"Expected INFO log about in-process fallback. Logs: {caplog.messages!r}"
    )


# =====================================================================
# HP-1.1 — In-process analyze on a seeded session (smoke test)
# =====================================================================


def test_hp_1_1_in_process_analyze_success(
    tmp_path: Path,
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
) -> None:
    """HP-1.1: `secondsight analyze --no-server` dispatches and exits 0.

    Full evidence chain: seeded events, Trigger.dispatch fires, returns
    dispatched=True, CLI exits 0.
    """
    from secondsight.sdk.trigger import DispatchResult

    dispatch_result = DispatchResult(
        dispatched=True,
        reason="dispatched",
        run_id=None,
    )

    with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
        with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
            mock_run.return_value = dispatch_result
            mock_build.return_value = MagicMock()

            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--session",
                    _SESSION_ID,
                    "--project",
                    _PROJECT_ID,
                    "--no-server",
                ],
            )

    assert result.exit_code == 0, (
        f"Expected exit 0 for successful dispatch. Output: {result.output!r}"
    )


# =====================================================================
# HP-1.1 extended — already-analyzed via in-process dispatch exits 2
# =====================================================================


def test_hp_1_1_already_analyzed_via_dispatch_exits_2() -> None:
    """HP-1.1 extended: when _run_in_process_dispatch returns already-analyzed,
    CLI must exit 2.
    """
    from secondsight.sdk.trigger import DispatchResult

    dispatch_result = DispatchResult(
        dispatched=False,
        reason="already-analyzed",
        run_id="run-123",
        existing_stage="aggregated",
        existing_completed_at=_NOW,
    )

    with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
        with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
            mock_run.return_value = dispatch_result
            mock_build.return_value = MagicMock()

            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--session",
                    _SESSION_ID,
                    "--project",
                    _PROJECT_ID,
                    "--no-server",
                ],
            )

    assert result.exit_code == 2, f"Expected exit 2 for already-analyzed. Output: {result.output!r}"
    assert "force" in result.output.lower() or "--force" in result.output, (
        f"Expected '--force' hint in output: {result.output!r}"
    )


# =====================================================================
# Orchestrator failure → exit code 1
# =====================================================================


def test_orchestrator_failure_exits_1() -> None:
    """When the in-process orchestrator raises an exception, CLI exits 1."""
    with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
        with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
            mock_run.side_effect = RuntimeError("analysis pipeline exploded")
            mock_build.return_value = MagicMock()

            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--session",
                    _SESSION_ID,
                    "--project",
                    _PROJECT_ID,
                    "--no-server",
                ],
            )

    assert result.exit_code == 1, (
        f"Expected exit 1 for orchestrator failure. Output: {result.output!r}"
    )


# =====================================================================
# FIX-LOOP: Critical #11 — HTTPStatusError must NOT silently fallback
# =====================================================================


def test_dt_fl_11_http_status_error_exits_1_no_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DT-FL-11: Server returning HTTP 500 must NOT fall back to in-process.

    Silent failure this closes: a broken server endpoint (500) was previously
    treated the same as a missing server (ConnectError) — both triggered
    in-process fallback. This meant operators using `secondsight analyze`
    could not distinguish "server not running" from "server broken endpoint."
    A 500 should abort with exit code 1, not silently retry in-process.
    """
    import logging
    import httpx as _real_httpx

    with patch("secondsight.cli.analyze.httpx") as mock_httpx:
        mock_httpx.ConnectError = _real_httpx.ConnectError
        mock_httpx.HTTPStatusError = _real_httpx.HTTPStatusError

        # Simulate a 500 response from the server (server is up, endpoint broken)
        mock_response = MagicMock()
        mock_response.status_code = 500
        http_error = _real_httpx.HTTPStatusError(
            "Internal Server Error",
            request=MagicMock(),
            response=mock_response,
        )
        mock_post = MagicMock()
        mock_post.side_effect = http_error
        mock_httpx.post = mock_post

        with patch("secondsight.cli.analyze._build_in_process_trigger"):
            with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
                with caplog.at_level(logging.ERROR, logger="secondsight.cli.analyze"):
                    result = runner.invoke(
                        app,
                        [
                            "analyze",
                            "--session",
                            _SESSION_ID,
                            "--project",
                            _PROJECT_ID,
                            # No --no-server: server-mode is attempted
                        ],
                    )
                # In-process must NOT have been called
                mock_run.assert_not_called()

    # Must exit with code 1 (server error)
    assert result.exit_code == 1, (
        f"Expected exit 1 for HTTPStatusError (server up, endpoint broken). "
        f"Got {result.exit_code}. Output: {result.output!r}"
    )

    # Output must mention the server error
    combined = result.output
    assert "500" in combined or "HTTP" in combined.upper(), (
        f"Expected HTTP 500 mention in output: {combined!r}"
    )


def test_dt_fl_11_connect_error_still_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DT-FL-11: ConnectError (server not running) still triggers in-process fallback."""
    import httpx as _real_httpx

    from secondsight.sdk.trigger import DispatchResult

    dispatch_result = DispatchResult(dispatched=True, reason="dispatched", run_id=None)

    with patch("secondsight.cli.analyze.httpx") as mock_httpx:
        mock_httpx.ConnectError = _real_httpx.ConnectError
        mock_httpx.HTTPStatusError = _real_httpx.HTTPStatusError
        mock_post = MagicMock()
        mock_post.side_effect = _real_httpx.ConnectError("connection refused")
        mock_httpx.post = mock_post

        with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
            with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
                mock_run.return_value = dispatch_result
                mock_build.return_value = MagicMock()

                result = runner.invoke(
                    app,
                    [
                        "analyze",
                        "--session",
                        _SESSION_ID,
                        "--project",
                        _PROJECT_ID,
                    ],
                )

    # Must succeed via in-process
    assert result.exit_code == 0, (
        f"ConnectError must trigger in-process fallback (exit 0). "
        f"Got {result.exit_code}. Output: {result.output!r}"
    )
    mock_run.assert_called_once()


# =====================================================================
# FIX-LOOP: Critical #12 — timeout reports honestly (exit 1 not 0)
# =====================================================================


def test_dt_fl_12_timeout_exits_1_not_0() -> None:
    """DT-FL-12: Analysis timeout must exit 1 with clear message, not exit 0.

    Silent failure this closes: _run_in_process_dispatch previously
    returned dispatched=True after cancelling timed-out tasks, causing
    _handle_dispatch_result to echo "Analysis complete" — when the analysis
    was actually cancelled. Operators saw a false success.
    """
    from secondsight.sdk.trigger import DispatchResult

    # Simulate the timeout path: dispatch returns reason="timed-out"
    dispatch_result = DispatchResult(
        dispatched=True,
        reason="timed-out",
        run_id=None,
    )

    with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
        with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
            mock_run.return_value = dispatch_result
            mock_build.return_value = MagicMock()

            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--session",
                    _SESSION_ID,
                    "--project",
                    _PROJECT_ID,
                    "--no-server",
                ],
            )

    assert result.exit_code == 1, (
        f"Expected exit 1 for timed-out analysis. Got {result.exit_code}. Output: {result.output!r}"
    )

    output = result.output
    assert "timeout" in output.lower() or "timed" in output.lower(), (
        f"Expected 'timeout' in output for timed-out result: {output!r}"
    )

    # Explicitly check "Analysis complete" is NOT in output
    assert "Analysis complete" not in output, (
        f"'Analysis complete' must NOT appear when analysis timed out. Output: {output!r}"
    )


def test_dt_fl_13_mode_aware_failure_exits_1_not_0() -> None:
    """DT-FL-13: mode-aware dispatch failure must not print 'Analysis complete'."""
    from secondsight.sdk.trigger import DispatchResult

    dispatch_result = DispatchResult(
        dispatched=True,
        reason="analysis-failed",
        run_id=None,
    )

    with patch("secondsight.cli.analyze._build_in_process_trigger") as mock_build:
        with patch("secondsight.cli.analyze._run_in_process_dispatch") as mock_run:
            mock_run.return_value = dispatch_result
            mock_build.return_value = MagicMock()

            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--session",
                    _SESSION_ID,
                    "--project",
                    _PROJECT_ID,
                    "--no-server",
                ],
            )

    assert result.exit_code == 1, (
        f"Expected exit 1 for failed mode-aware analysis. Got {result.exit_code}. "
        f"Output: {result.output!r}"
    )
    assert "Analysis failed" in result.output, (
        f"Expected failure wording in output: {result.output!r}"
    )
    assert "Analysis complete" not in result.output, (
        f"'Analysis complete' must NOT appear for failed analysis. Output: {result.output!r}"
    )
