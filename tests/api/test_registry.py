"""Death tests + unit tests for ProjectRegistry.

Death tests (must go RED before production code):
  DT-2: Concurrent-init race for the same new project_id — only ONE DBEngine constructed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from secondsight.api.registry import ProjectRegistry


# ---------------------------------------------------------------------------
# DT-2: Concurrent-init race — exactly one DBEngine per project_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_death_concurrent_init_race_single_dbengine(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: 50 concurrent get() calls for a new project must produce
    exactly ONE DBEngine. If the registry has no per-project locking, multiple
    DBEngine instances may be created, potentially creating WAL race conditions
    on the same SQLite file.
    """
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    construction_count = 0
    original_init = None

    try:
        from secondsight.storage.db_engine import DBEngine

        original_init = DBEngine.__init__

        def counting_init(self, db_path, settings=None):  # type: ignore[override]
            nonlocal construction_count
            construction_count += 1
            return original_init(self, db_path, settings)

        with patch.object(DBEngine, "__init__", counting_init):
            # 50 concurrent requests for the same brand-new project
            results = await asyncio.gather(*[registry.get("concurrent-proj-x") for _ in range(50)])

        # All 50 calls must return the SAME ProjectResources object
        first_id = id(results[0].db_engine)
        for i, res in enumerate(results):
            assert id(res.db_engine) == first_id, f"Call {i} returned a different DBEngine instance"

        # Only ONE DBEngine should have been constructed
        assert construction_count == 1, (
            f"Expected 1 DBEngine construction, got {construction_count}. "
            "Registry has a concurrent-init race condition."
        )
    finally:
        await registry.aclose()


# ---------------------------------------------------------------------------
# Unit test: idempotent get (1000 sequential calls → 1 DBEngine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_get_idempotent_sequential(
    tmp_secondsight_home: Path,
) -> None:
    """Unit: 1000 sequential calls for the same project_id hit DBEngine.__init__
    exactly once.
    """
    from secondsight.storage.db_engine import DBEngine

    construction_count = 0
    original_init = DBEngine.__init__

    def counting_init(self, db_path, settings=None):  # type: ignore[override]
        nonlocal construction_count
        construction_count += 1
        return original_init(self, db_path, settings)

    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    try:
        with patch.object(DBEngine, "__init__", counting_init):
            for _ in range(1000):
                await registry.get("idempotent-proj")

        assert construction_count == 1, (
            f"Expected 1 DBEngine construction, got {construction_count}"
        )
    finally:
        await registry.aclose()


# ---------------------------------------------------------------------------
# Unit test: aclose is idempotent (two calls do not raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_aclose_idempotent(tmp_secondsight_home: Path) -> None:
    """Unit: calling aclose() twice must not raise."""
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    await registry.get("test-proj")
    await registry.aclose()
    # Second call must not raise
    await registry.aclose()


# ---------------------------------------------------------------------------
# Unit test: get returns ProjectResources with all required fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_get_returns_project_resources(
    tmp_secondsight_home: Path,
) -> None:
    """Unit: get() returns a ProjectResources with all required attributes."""
    from secondsight.storage.events_repository import EventsRepository
    from secondsight.storage.raw_ingress_store import RawIngressStore
    from secondsight.storage.raw_trace_store import RawTraceStore
    from secondsight.storage.sync_log import SyncLog
    from secondsight.observation.pipeline import ObservationPipeline
    from secondsight.storage.db_engine import DBEngine

    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    try:
        res = await registry.get("full-resource-proj")
        assert res.project_id == "full-resource-proj"
        assert isinstance(res.db_engine, DBEngine)
        assert isinstance(res.events_repository, EventsRepository)
        assert isinstance(res.raw_ingress_store, RawIngressStore)
        assert isinstance(res.raw_trace_store, RawTraceStore)
        assert isinstance(res.sync_log, SyncLog)
        assert isinstance(res.pipeline, ObservationPipeline)
    finally:
        await registry.aclose()


# ---------------------------------------------------------------------------
# Unit test: two different projects get different resources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_different_projects_different_resources(
    tmp_secondsight_home: Path,
) -> None:
    """Unit: two distinct project_ids get distinct DBEngine instances."""
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    try:
        res_a = await registry.get("project-alpha")
        res_b = await registry.get("project-beta")
        assert res_a.db_engine is not res_b.db_engine
        assert res_a.project_id != res_b.project_id
    finally:
        await registry.aclose()
