"""Runtime loguru configuration helpers.

This module owns the process-level stderr sink used by production CLI/server
entrypoints. The config loader already resolves ``[general].log_level``; this
module is the missing consumer that applies that value to loguru.
"""

from __future__ import annotations

import sys

from loguru import logger

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})
_DEFAULT_LOGURU_SINK_ID = 0
_managed_stderr_sink_id: int | None = None


def configure_logging(level: str) -> str:
    """Apply a process-wide stderr sink level for loguru and return it.

    The first call removes loguru's built-in default stderr sink (id=0) so its
    hard-coded DEBUG threshold cannot leak messages past the configured level.
    Subsequent calls only replace the sink managed by this module, leaving any
    test-only or ad-hoc sinks intact.

    Invalid values degrade to ``"info"`` instead of crashing server startup.
    """
    global _managed_stderr_sink_id

    normalized = str(level).strip().lower()
    if normalized not in _VALID_LOG_LEVELS:
        normalized = "info"

    try:
        logger.remove(_DEFAULT_LOGURU_SINK_ID)
    except ValueError:
        pass

    if _managed_stderr_sink_id is not None:
        try:
            logger.remove(_managed_stderr_sink_id)
        except ValueError:
            pass

    _managed_stderr_sink_id = logger.add(sys.stderr, level=normalized.upper())
    return normalized


__all__ = ["configure_logging"]
