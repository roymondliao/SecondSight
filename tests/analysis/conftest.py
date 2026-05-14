"""Shared fixtures and helpers for analysis tests.

Centralizes helpers that were previously duplicated across multiple test files:
  - _make_retention() was duplicated in test_runtime_dispatch_mode.py and
    test_runtime_concurrent_dispatch.py (IMPORTANT FIX 9).
"""

from __future__ import annotations

import pytest

from secondsight.storage.retention import RetentionConfig


def make_retention() -> RetentionConfig:
    """Create a default RetentionConfig for tests.

    Previously duplicated as _make_retention() in:
      - tests/analysis/test_runtime_dispatch_mode.py
      - tests/analysis/test_runtime_concurrent_dispatch.py
      - tests/analysis/test_runtime_wiring_death.py
      - tests/analysis/test_runtime_wiring_unit.py

    Centralized here per IMPORTANT FIX 9 (DRY violation).
    Existing test files keep their local _make_retention() copies for
    backward compatibility — callers that use the local copy are not broken.
    New tests should import make_retention from conftest instead.
    """
    return RetentionConfig(
        raw_traces_ttl_days=30,
        raw_traces_source="builtin_default",
        analysis_ttl_days=90,
        analysis_ttl_source="builtin_default",
        cleanup_after_analysis=False,
    )


@pytest.fixture
def default_retention() -> RetentionConfig:
    """Pytest fixture version of make_retention() for injection."""
    return make_retention()
