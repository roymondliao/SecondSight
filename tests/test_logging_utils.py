"""Logging runtime wiring tests."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loguru import logger

from secondsight.logging_utils import configure_logging


def _config(*, log_level: str = "info"):
    return SimpleNamespace(general=SimpleNamespace(log_level=log_level))


def test_configure_logging_info_suppresses_debug_output(monkeypatch) -> None:
    """DT-log-1: [general].log_level=info must suppress loguru DEBUG on stderr."""
    real_stderr = sys.stderr
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stderr", sink)

    configure_logging("info")
    logger.debug("probe-debug-should-not-appear")
    logger.info("probe-info-should-appear")

    output = sink.getvalue()
    assert "probe-debug-should-not-appear" not in output, (
        "DEBUG log leaked through even though log_level=info was configured."
    )
    assert "probe-info-should-appear" in output, "INFO log was unexpectedly suppressed."

    monkeypatch.setattr(sys, "stderr", real_stderr)
    configure_logging("debug")


def test_run_server_passes_configured_log_level_to_uvicorn() -> None:
    """UT-log-2: serve runtime must align uvicorn's stdlib log level too."""
    from secondsight.cli.serve import _run_server

    fake_app = object()
    with (
        patch("secondsight.cli.serve.create_app", return_value=fake_app),
        patch("secondsight.cli.serve.uvicorn.run") as uvicorn_run,
        patch("secondsight.cli.serve.configure_logging", return_value="warning") as configure_mock,
    ):
        _run_server(Path("/tmp/secondsight-home"), _config(log_level="warning"))

    configure_mock.assert_called_with("warning")
    uvicorn_run.assert_called_once()
    assert uvicorn_run.call_args.kwargs["log_level"] == "warning"
