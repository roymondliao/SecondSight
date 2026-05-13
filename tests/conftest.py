"""Shared test fixtures for production storage tests (NOT poc/)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from _pytest.logging import LogCaptureFixture
from loguru import logger as _loguru_logger

from secondsight.event import Event, EventType


@pytest.fixture
def caplog(caplog: LogCaptureFixture) -> Iterator[LogCaptureFixture]:
    """Bridge loguru output into pytest's stdlib-only caplog fixture.

    Without this, `from loguru import logger; logger.info(...)` bypasses
    caplog because loguru does not route through stdlib's root logger.
    Adds caplog's stdlib Handler as a loguru sink for the test's duration,
    so existing `caplog.at_level(...) / r.message` assertions continue
    to work uniformly across both legacy stdlib-logging code and
    loguru-emitting code.
    """
    handler_id = _loguru_logger.add(
        caplog.handler,
        format="{message}",
        level=0,
        filter=lambda record: record["level"].no >= caplog.handler.level,
    )
    yield caplog
    _loguru_logger.remove(handler_id)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Per-project root directory (mirrors ~/.secondsight/projects/{pid}/)."""
    root = tmp_path / "project_alpha"
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_event(
    *,
    event_id: str = "evt-0001",
    session_id: str = "sess-001",
    project_id: str = "proj-alpha",
    event_type: EventType = EventType.USER_PROMPT,
    timestamp: datetime | None = None,
    sequence_number: int = 1,
    segment_index: int = 0,
    sub_agent_id: str | None = None,
    depth: int = 0,
    duration_ms: int | None = None,
    token_count: int | None = None,
    data: dict | None = None,
) -> Event:
    """Construct a minimal valid Event for tests."""
    return Event(
        id=event_id,
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=timestamp or datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc),
        sequence_number=sequence_number,
        segment_index=segment_index,
        sub_agent_id=sub_agent_id,
        depth=depth,
        duration_ms=duration_ms,
        token_count=token_count,
        data=data or {"prompt_text": "hello"},
    )
