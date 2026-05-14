"""Shared fixtures for API tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from loguru import logger


@pytest.fixture
def tmp_secondsight_home(tmp_path: Path) -> Path:
    """A fresh SecondSight home directory for each test.

    Task 5: includes a test-only Anthropic provider key in config.toml so that
    LLMRouter construction passes at analysis runtime build time. The key is a
    placeholder — no real API calls are made in API tests (mocked dispatchers).

    Task 6 review (CRITICAL FIX 1): mode is set to "sdk" so that ModeAwareDispatch
    routes to SDKAnalysisDispatcher (mockable) rather than CLIAnalysisDispatcher
    (requires system PATH binary). API tests that mock _build_analysis_agent work
    because the SDK path goes through PydanticAIAnalysisAgent, not CLI subprocess.
    """
    home = tmp_path / ".secondsight"
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(exist_ok=True)
    # Ensure LLMRouter's provider key validation passes during runtime build.
    # mode = "sdk": tests use SDK path (mockable via _build_analysis_agent patch).
    (home / "config.toml").write_text(
        "[general]\n"
        'mode = "sdk"\n'
        "\n"
        "[analysis.sdk]\n"
        'primary_model = "claude-haiku-4-5-20251001"\n'
        "\n"
        "[providers.anthropic]\n"
        'ANTHROPIC_API_KEY = "sk-test-api-fixture-placeholder"\n'
    )
    return home


@pytest.fixture
def propagate_loguru_to_caplog(caplog: pytest.LogCaptureFixture):
    """Wire loguru to propagate to pytest's caplog fixture.

    By default loguru does not propagate to the stdlib logging system that
    caplog intercepts. This fixture adds a temporary loguru sink that feeds
    into the root stdlib logger so pytest caplog can capture it.

    Usage: include `propagate_loguru_to_caplog` in any test that needs to
    assert on loguru output via caplog.
    """
    root_logger = logging.getLogger()

    def _loguru_to_root(message) -> None:  # type: ignore[type-arg]
        record = message.record
        level_name = record["level"].name
        level_no = getattr(logging, level_name, logging.DEBUG)
        # Log to root logger using the loguru module name for traceability
        logging.getLogger(record["name"]).log(level_no, record["message"])

    sink_id = logger.add(_loguru_to_root, format="{message}")
    # Ensure root logger captures at DEBUG+ so caplog.at_level() controls
    # the filter, not the root logger's own level.
    old_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)

    yield caplog

    logger.remove(sink_id)
    root_logger.setLevel(old_level)
