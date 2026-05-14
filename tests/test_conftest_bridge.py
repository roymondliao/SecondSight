"""Meta-test: verifies the loguru->caplog bridge fixture in conftest.py is wired.

If this test fails, the conftest caplog fixture is broken. ALL tests that assert
on loguru log output via caplog will silently lose coverage. Do NOT skip this
test to "fix" a failure -- investigate the bridge fixture in tests/conftest.py.

The bridge works by registering caplog's stdlib Handler as a loguru sink for the
duration of each test (handler_id = loguru_logger.add(caplog.handler, ...)). If
that add() call is removed or broken, loguru emissions bypass caplog.records
entirely and any assert on caplog.records silently passes vacuously (empty list).

Silent failure path guarded by this test:
  - conftest.py caplog fixture loses the loguru_logger.add() call
  - DC12 warning tests in tests/config/test_loader_v2.py (TestDTV2Loader1LegacyFlatAnalysis)
    assert `any(matching)` where matching is filtered from caplog.records
  - With empty records, matching=[], any([])=False -> assert fails LOUDLY today
  - BUT if a future maintainer changes the assert to pytest.skip or weakens it,
    the bridge breakage becomes permanently invisible
  - This meta-test fails FIRST with an actionable message, before DC12 tests run
"""

from __future__ import annotations

import pytest
from loguru import logger


def test_loguru_caplog_bridge_is_wired(caplog: pytest.LogCaptureFixture) -> None:
    """Probe the loguru->caplog bridge. If this fails, conftest caplog fixture is broken.

    This test emits a uniquely-keyed loguru warning and asserts it appears in
    caplog.records. The probe message is chosen to be collision-resistant
    (unlikely to appear in real application logs).

    If this test fails: check tests/conftest.py for the caplog fixture override
    that calls loguru_logger.add(caplog.handler, ...). That add() call must exist
    and must not be skipped, conditioned out, or raise before yielding.
    """
    PROBE_MSG = "__loguru_caplog_bridge_probe_f5__"

    with caplog.at_level("WARNING"):
        logger.warning(PROBE_MSG)

    captured_messages = [r.message for r in caplog.records]
    assert any(PROBE_MSG in msg for msg in captured_messages), (
        f"loguru->caplog bridge is NOT wired. "
        f"Expected probe message {PROBE_MSG!r} in caplog.records, "
        f"got: {captured_messages!r}. "
        f"Check tests/conftest.py -- the caplog fixture override must call "
        f"loguru_logger.add(caplog.handler, ...) before yielding. "
        f"Do NOT skip or weaken this test -- fix the bridge."
    )
