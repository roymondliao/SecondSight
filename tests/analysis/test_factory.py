"""Death + happy-path tests for build_orchestrator factory — task-B6 of GUR-149.

Samsara discipline: death tests first.

Death cases (from changes/2026-05-07_gur-149_analysis-ttl-and-post-analysis-trigger/2-plan.md §3):

    DC-B4: Boot-time guard. Operator configures
           `[retention].cleanup_after_analysis = true` in their TOML, but a
           future refactor of the factory drops the trigger wiring (e.g.,
           a programmer accidentally returns `Orchestrator(..., on_analysis_complete=None)`
           on the enabled path). Without this guard, the operator's
           opt-in is silently disabled — they see no errors at boot, no
           ERROR logs at runtime, and no eager cleanup ever fires.

           Detection contract (per task-B6.md "Recommendation: (B) raise"):
           the factory's wiring-consistency assertion raises RuntimeError
           with a message naming the missing wire. This is fail-loud
           because silent disablement is the silent-failure pattern this
           whole ticket exists to close.

Production caller status (documented gap):
    No production code currently calls `build_orchestrator`. The factory
    exists as the canonical wiring helper that future entry points
    (CLI/HTTP analyze triggers) will use. Until those entry points land,
    the factory is dead-but-correct: tests pin its contract; nothing
    invokes it. This is the documented gap from task-B6.md, surfaced as
    scar-B6-1.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pytest

from secondsight.analysis.factory import (
    OrchestratorWiringError,
    build_orchestrator,
)
from secondsight.analysis.orchestrator import Orchestrator
from secondsight.analysis.post_analysis_cleanup import PostAnalysisCleanupTrigger
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.session_reports_repository import SessionReportsRepository
from tests.analysis._fake_agent import FakeAnalysisAgent

UTC = timezone.utc


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    monkeypatch.setenv("SECONDSIGHT_HOME", str(h))
    return h


@pytest.fixture
def project_id() -> str:
    return "proj-factory-test"


@pytest.fixture
def project_resources(home: Path, project_id: str):
    """Build per-project resources via the same path the production
    factory uses, then wire the analysis-side schemas the factory needs."""
    from secondsight.api.registry import ProjectRegistry

    registry = ProjectRegistry(secondsight_home=home)
    resources = registry._build_resources(project_id)  # noqa: SLF001
    # Eager schema creation so factory tests don't have to.
    SessionReportsRepository(resources.db_engine).create_schema()
    BehaviorFlagsRepository(resources.db_engine).create_schema()
    DirectivesRepository(resources.db_engine).create_schema()
    AnalysisRunsRepository(resources.db_engine).create_schema()
    yield resources
    resources.db_engine.dispose()


@pytest.fixture
def fake_agent() -> FakeAnalysisAgent:
    return FakeAnalysisAgent(
        segment_outputs=[],
        summary_output=None,
    )


# ======================================================================
# DC-B4 — Boot-time guard
# ======================================================================


class TestDcB4BootTimeGuard:
    """Pin the wiring-consistency contract: when
    `cleanup_after_analysis=True` is resolved from config, the factory
    MUST end up with a non-None `on_analysis_complete` on the
    Orchestrator. A drop in the wiring path raises
    OrchestratorWiringError naming the missing wire."""

    def test_factory_raises_when_config_enabled_but_trigger_builder_returns_none(
        self,
        home: Path,
        project_id: str,
        project_resources,
        fake_agent: FakeAnalysisAgent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator opts in via config.
        proj_cfg = home / "projects" / project_id / "config.toml"
        proj_cfg.write_text("[retention]\ncleanup_after_analysis = true\n")

        # Simulate a wiring drop: monkeypatch the trigger-builder helper
        # so it returns None even though config says enabled.
        from secondsight.analysis import factory as factory_module

        def broken_trigger_builder(*args, **kwargs):  # type: ignore[no-untyped-def]
            return None

        monkeypatch.setattr(
            factory_module,
            "_build_cleanup_trigger",
            broken_trigger_builder,
        )

        with pytest.raises(OrchestratorWiringError) as exc_info:
            build_orchestrator(
                home=home,
                project_id=project_id,
                resources=project_resources,
                agent=fake_agent,
            )

        # Error message must name the missing wire so an operator can
        # locate the inconsistency without reading the source. Strict
        # contract: BOTH the config field name AND the orchestrator
        # callback name must appear (yin review B6 fix: was OR-loose,
        # tightened to AND so a future message refactor cannot silently
        # drop the on_analysis_complete reference).
        msg = str(exc_info.value)
        assert "cleanup_after_analysis" in msg
        assert "on_analysis_complete" in msg


# ======================================================================
# Happy paths
# ======================================================================


class TestFactoryDisabledPath:
    def test_factory_with_default_config_returns_orchestrator_without_callback(
        self,
        home: Path,
        project_id: str,
        project_resources,
        fake_agent: FakeAnalysisAgent,
    ) -> None:
        """No config at all → cleanup_after_analysis defaults to False
        → orchestrator built with on_analysis_complete=None."""
        orch = build_orchestrator(
            home=home,
            project_id=project_id,
            resources=project_resources,
            agent=fake_agent,
        )
        assert isinstance(orch, Orchestrator)
        # Private attribute access is the only way to verify the wire
        # actually IS None (the Orchestrator API doesn't expose it).
        assert orch._on_analysis_complete is None  # noqa: SLF001


class TestFactoryEnabledPath:
    def test_factory_with_cleanup_after_analysis_wires_trigger(
        self,
        home: Path,
        project_id: str,
        project_resources,
        fake_agent: FakeAnalysisAgent,
    ) -> None:
        """Config enables cleanup_after_analysis → factory builds
        PostAnalysisCleanupTrigger and passes it as the orchestrator's
        on_analysis_complete callback."""
        proj_cfg = home / "projects" / project_id / "config.toml"
        proj_cfg.write_text("[retention]\ncleanup_after_analysis = true\n")

        orch = build_orchestrator(
            home=home,
            project_id=project_id,
            resources=project_resources,
            agent=fake_agent,
        )

        # Wire is a PostAnalysisCleanupTrigger.
        trigger = orch._on_analysis_complete  # noqa: SLF001
        assert isinstance(trigger, PostAnalysisCleanupTrigger)


class TestFactoryNonBoolConfigRaises:
    """Boundary: a bool-typed config field must reject non-bool values
    rather than silently coerce. Mirrors raw_traces_ttl_days's
    rejection of bools-as-ints."""

    def test_string_value_for_cleanup_after_analysis_raises(
        self,
        home: Path,
        project_id: str,
        project_resources,
        fake_agent: FakeAnalysisAgent,
    ) -> None:
        from secondsight.storage.retention import RetentionConfigError

        proj_cfg = home / "projects" / project_id / "config.toml"
        proj_cfg.write_text('[retention]\ncleanup_after_analysis = "yes"\n')
        with pytest.raises(RetentionConfigError) as exc_info:
            build_orchestrator(
                home=home,
                project_id=project_id,
                resources=project_resources,
                agent=fake_agent,
            )
        assert "cleanup_after_analysis" in str(exc_info.value)
