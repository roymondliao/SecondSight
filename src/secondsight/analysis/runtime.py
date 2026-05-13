"""Shared analysis runtime builder for server and CLI paths.

This module centralizes the per-project analysis assembly that used to live
only in the CLI path. The server uses the same builder so event-driven
dispatch and timeout recovery share one canonical runtime shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from secondsight.analysis.config import AnalysisConfig
from secondsight.analysis.factory import build_orchestrator
from secondsight.analysis.orchestrator import Orchestrator
from secondsight.analysis.tools import AnalysisTools
from secondsight.config import load_project_config
from secondsight.sdk.agent import PydanticAIAnalysisAgent
from secondsight.sdk.model_selection import select_model
from secondsight.sdk.router import LLMRouter
from secondsight.sdk.trigger import LockRegistry, Trigger
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.session_reports_repository import SessionReportsRepository

if TYPE_CHECKING:
    from secondsight.api.registry import ProjectResources


@dataclass(frozen=True)
class ProjectAnalysisRuntime:
    """Per-project analysis-side runtime owned by the server/CLI caller."""

    analysis_runs_repository: AnalysisRunsRepository
    behavior_flags_repository: BehaviorFlagsRepository
    directives_repository: DirectivesRepository
    session_reports_repository: SessionReportsRepository
    orchestrator: Orchestrator
    trigger: Trigger


@dataclass(frozen=True)
class _OrchestratorResources:
    """Minimal resource bundle required by analysis.factory.build_orchestrator()."""

    db_engine: DBEngine
    events_repository: EventsRepository
    raw_trace_store: RawTraceStore


def _build_analysis_agent(
    *,
    secondsight_home: Path,
    project_id: str,
    events_repository: EventsRepository,
    flags_repository: BehaviorFlagsRepository,
    directives_repository: DirectivesRepository,
) -> PydanticAIAnalysisAgent:
    """Build the analysis agent chain for one project."""
    project_dir = Path(secondsight_home) / "projects" / project_id
    project_config_path = project_dir / "config.toml"

    analysis_config = AnalysisConfig.load(config_path=project_config_path)
    cfg = load_project_config(home=Path(secondsight_home), project_id=project_id)

    # select_model() uses structural typing: expects project_config.analysis.model
    # and global_config.analysis.{default_agent, models.*}. SecondSightConfig does NOT
    # match this shape directly (cfg.project_analysis.model, not cfg.analysis.model).
    # SimpleNamespace wrappers remap to the expected shape:
    #   project_config = SimpleNamespace(analysis=cfg.project_analysis)
    #   global_config  = SimpleNamespace(analysis=cfg.analysis)
    # If SecondSightConfig field names change, these wrappers will silently break.
    # The death tests in tests/config/test_runtime_wiring.py catch this regression.
    primary, fallbacks = select_model(
        project_id=project_id,
        project_config=SimpleNamespace(analysis=cfg.project_analysis),
        global_config=SimpleNamespace(analysis=cfg.analysis),
        events_repo=events_repository,
    )

    router = LLMRouter(primary=primary, fallbacks=fallbacks)
    tools = AnalysisTools(
        events_repo=events_repository,
        flags_repo=flags_repository,
        directives_repo=directives_repository,
        project_root=project_dir,
        extra_denylist=analysis_config.extra_denylist,
        size_cap_bytes=analysis_config.size_cap_kb * 1024,
        read_project_file_enabled=analysis_config.read_project_file_enabled,
    )
    return PydanticAIAnalysisAgent(router=router, tools=tools)


def build_project_analysis_runtime(
    *,
    secondsight_home: Path,
    project_id: str,
    db_engine: DBEngine,
    events_repository: EventsRepository,
    raw_trace_store: RawTraceStore,
) -> ProjectAnalysisRuntime:
    """Build and return the shared per-project analysis runtime."""
    runs_repo = AnalysisRunsRepository(db_engine)
    flags_repo = BehaviorFlagsRepository(db_engine)
    directives_repo = DirectivesRepository(db_engine)
    reports_repo = SessionReportsRepository(db_engine)

    runs_repo.create_schema()
    flags_repo.create_schema()
    directives_repo.create_schema()
    reports_repo.create_schema()

    agent = _build_analysis_agent(
        secondsight_home=secondsight_home,
        project_id=project_id,
        events_repository=events_repository,
        flags_repository=flags_repo,
        directives_repository=directives_repo,
    )
    orchestrator = build_orchestrator(
        home=secondsight_home,
        project_id=project_id,
        resources=cast(
            "ProjectResources",
            _OrchestratorResources(
                db_engine=db_engine,
                events_repository=events_repository,
                raw_trace_store=raw_trace_store,
            ),
        ),
        agent=agent,
    )
    trigger = Trigger(
        orchestrator=orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repository,
        lock_registry=LockRegistry(),
    )

    return ProjectAnalysisRuntime(
        analysis_runs_repository=runs_repo,
        behavior_flags_repository=flags_repo,
        directives_repository=directives_repo,
        session_reports_repository=reports_repo,
        orchestrator=orchestrator,
        trigger=trigger,
    )


__all__ = ["ProjectAnalysisRuntime", "build_project_analysis_runtime"]
