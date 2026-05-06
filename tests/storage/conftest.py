"""Shared fixtures for storage tests.

`propagate_loguru_to_caplog` mirrors the API conftest fixture so storage
tests (specifically the RawTracesPurger DC-5 / FS-failure cases in
test_retention_purger.py) can assert on loguru ERROR lines via pytest's
caplog. Loguru does not propagate to stdlib logging by default; without
this bridge, caplog never sees the records the purger emits.
"""

from __future__ import annotations

import logging

import pytest
from loguru import logger


@pytest.fixture
def propagate_loguru_to_caplog(caplog: pytest.LogCaptureFixture):
    root_logger = logging.getLogger()

    def _loguru_to_root(message) -> None:  # type: ignore[type-arg]
        record = message.record
        level_name = record["level"].name
        level_no = getattr(logging, level_name, logging.DEBUG)
        logging.getLogger(record["name"]).log(level_no, record["message"])

    sink_id = logger.add(_loguru_to_root, format="{message}")
    old_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)

    yield caplog

    logger.remove(sink_id)
    root_logger.setLevel(old_level)
