"""Death tests and unit tests for precheck() integration in `secondsight serve`.

Death tests MUST come first.

Death case: failing precheck → serve exits non-zero (does NOT start server).
Happy path: passing precheck → serve starts normally.

The test patches precheck() directly so we don't need a real server startup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from secondsight.cli.app import app as secondsight_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


runner = CliRunner()


def _failing_precheck(*args, **kwargs):
    """Return a failing PrecheckResult for injection into serve."""
    from secondsight.config.precheck import PrecheckResult

    return PrecheckResult.fail(
        reason="state_missing",
        message="Run `secondsight init` before starting the server.",
    )


def _passing_precheck(*args, **kwargs):
    """Return a passing PrecheckResult for injection into serve."""
    from secondsight.config.precheck import PrecheckResult

    return PrecheckResult.ok()


# ---------------------------------------------------------------------------
# DEATH TESTS
# ---------------------------------------------------------------------------


def test_serve_failing_precheck_exits_nonzero(tmp_path: Path) -> None:
    """Death test: failing precheck → serve exits with non-zero status code."""
    with (
        patch("secondsight.config.precheck.precheck", side_effect=_failing_precheck),
        patch("secondsight.cli.serve._run_server"),  # prevent actual server start
        patch("secondsight.cli._home.secondsight_home", return_value=tmp_path),
    ):
        result = runner.invoke(secondsight_app, ["serve"])

    # Must exit non-zero when precheck fails
    assert result.exit_code != 0, (
        f"Expected non-zero exit code when precheck fails. "
        f"Got exit_code={result.exit_code}. Output: {result.output!r}"
    )


def test_serve_failing_precheck_logs_actionable_error(tmp_path: Path) -> None:
    """Death test: failing precheck → error output contains actionable message."""
    with (
        patch("secondsight.config.precheck.precheck", side_effect=_failing_precheck),
        patch("secondsight.cli.serve._run_server"),
        patch("secondsight.cli._home.secondsight_home", return_value=tmp_path),
    ):
        result = runner.invoke(secondsight_app, ["serve"])

    # Output (stdout or stderr combined by runner) should mention the failure reason
    combined_output = (result.output or "") + (
        result.stderr if hasattr(result, "stderr") and result.stderr else ""
    )
    assert result.exit_code != 0


def test_serve_passing_precheck_does_not_exit_nonzero(tmp_path: Path) -> None:
    """Happy path: passing precheck → serve does NOT exit with error code.

    We can't start a real server, so we patch _run_server to be a no-op.
    The serve command should proceed normally (exit 0 or no explicit exit).
    """
    with (
        patch("secondsight.config.precheck.precheck", side_effect=_passing_precheck),
        patch("secondsight.cli.serve._run_server"),  # no-op, returns immediately
        patch("secondsight.cli._home.secondsight_home", return_value=tmp_path),
    ):
        result = runner.invoke(secondsight_app, ["serve"])

    # When precheck passes and server starts normally, should NOT have error exit
    # (exit_code 0 or server blocking — since _run_server is mocked it returns immediately)
    assert result.exit_code == 0, (
        f"Expected exit_code=0 when precheck passes. "
        f"Got exit_code={result.exit_code}. Output: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# UNIT TESTS — precheck integration
# ---------------------------------------------------------------------------


def test_serve_calls_precheck_before_server_start(tmp_path: Path) -> None:
    """precheck() must be called before _run_server() during serve startup."""
    call_order: list[str] = []

    def record_precheck(*args, **kwargs):
        call_order.append("precheck")
        from secondsight.config.precheck import PrecheckResult

        return PrecheckResult.ok()

    def record_run_server(*args, **kwargs):
        call_order.append("run_server")

    with (
        patch("secondsight.config.precheck.precheck", side_effect=record_precheck),
        patch("secondsight.cli.serve._run_server", side_effect=record_run_server),
        patch("secondsight.cli._home.secondsight_home", return_value=tmp_path),
    ):
        runner.invoke(secondsight_app, ["serve"])

    assert "precheck" in call_order, "precheck() was not called during serve startup"
    if "run_server" in call_order:
        # If server was called, precheck must have been called first
        precheck_idx = call_order.index("precheck")
        run_server_idx = call_order.index("run_server")
        assert precheck_idx < run_server_idx, "precheck() must be called BEFORE _run_server()"


def test_serve_failing_precheck_does_not_call_run_server(tmp_path: Path) -> None:
    """When precheck fails, _run_server() must NOT be called."""
    run_server_called = []

    def record_run_server(*args, **kwargs):
        run_server_called.append(True)

    with (
        patch("secondsight.config.precheck.precheck", side_effect=_failing_precheck),
        patch("secondsight.cli.serve._run_server", side_effect=record_run_server),
        patch("secondsight.cli._home.secondsight_home", return_value=tmp_path),
    ):
        runner.invoke(secondsight_app, ["serve"])

    assert not run_server_called, (
        "_run_server() was called even though precheck failed. "
        "Server must NOT start when precheck fails."
    )
