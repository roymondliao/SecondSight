"""Orchestrator factory — task-B6 of GUR-149.

Canonical wiring helper for constructing an :class:`Orchestrator` with
the correct retention-aware callback wiring. Reads
:class:`RetentionConfig` and, when ``cleanup_after_analysis = true``,
constructs a :class:`PostAnalysisCleanupTrigger` and passes it as the
orchestrator's ``on_analysis_complete`` callback.

DC-B4 boot-time guard:
    The factory raises :class:`OrchestratorWiringError` (a subclass of
    :class:`RuntimeError`) when the resolved config says
    ``cleanup_after_analysis=True`` but the wiring path produces a None
    callback. This is fail-loud at construction time so an operator who
    opted into eager cleanup sees the misuse at boot, not after weeks of
    silently skipped post-analysis cleanups.

    The chosen policy is "raise" rather than "warn-and-continue" (per
    task-B6.md "Recommendation: (B) raise"), because silent disablement
    is the silent-failure pattern this whole ticket exists to close.

Production caller status (documented gap, scar-B6-1):
    No production code currently calls ``build_orchestrator``. The
    orchestrator has no production entry point yet — it is constructed
    only in tests today (verified via ``Grep -p Orchestrator\\(``). The
    factory exists as the seam future entry points will use; until those
    arrive, it is dead-but-correct: tests pin its contract; nothing
    invokes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from secondsight.analysis.orchestrator import Orchestrator
from secondsight.analysis.post_analysis_cleanup import PostAnalysisCleanupTrigger
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.retention import RawTracesPurger, RetentionConfig
from secondsight.storage.session_reports_repository import SessionReportsRepository

if TYPE_CHECKING:
    from secondsight.analysis.agent import AnalysisAgent
    from secondsight.api.registry import ProjectResources


class OrchestratorWiringError(RuntimeError):
    """Raised when the factory's resolved config contradicts its
    actual wiring (DC-B4).

    Concrete trigger condition: ``RetentionConfig.cleanup_after_analysis``
    is True but the trigger-builder returned None. This indicates a
    factory-internal bug (refactor accidentally dropped the wiring) or
    a future code path that bypasses the factory's normal flow.
    """


def _build_cleanup_trigger(
    *,
    config: RetentionConfig,
    resources: ProjectResources,
) -> PostAnalysisCleanupTrigger | None:
    """Construct a :class:`PostAnalysisCleanupTrigger` if the config
    enables it; return None otherwise.

    Extracted as a separate helper specifically so the DC-B4 test can
    monkeypatch this function with a broken-builder spy that returns
    None even when the config says enabled, simulating a wiring drop.
    """
    if not config.cleanup_after_analysis:
        return None
    # Marked bet (quality review B6 O fix): `resources.events_repository`
    # serves BOTH the purger's DB-delete path AND the trigger's event-
    # lookup path. This is sound today because ProjectResources bundles
    # one project's engine, but if a future refactor splits raw-trace
    # storage from event storage, this wiring would silently target two
    # different scopes. The two callees MUST share the same project DB.
    purger = RawTracesPurger(
        repo=resources.events_repository,
        raw_trace_store=resources.raw_trace_store,
    )
    return PostAnalysisCleanupTrigger(
        cleanup_after_analysis=True,
        raw_traces_purger=purger,
        events_repo=resources.events_repository,
    )


def build_orchestrator(
    *,
    home: Path,
    project_id: str,
    resources: ProjectResources,
    agent: AnalysisAgent,
) -> Orchestrator:
    """Construct an Orchestrator wired with the correct retention callback.

    Args:
        home: SecondSight home directory.
        project_id: Project identifier (used to resolve per-project config).
        resources: Pre-built per-project resources (events repo, behavior
            flags repo, etc.). Caller owns the DBEngine lifecycle —
            the factory does NOT dispose.
        agent: AnalysisAgent implementation.

    Returns:
        An Orchestrator with ``on_analysis_complete`` set to a
        PostAnalysisCleanupTrigger when
        ``[retention].cleanup_after_analysis = true``, or None
        otherwise.

    Raises:
        OrchestratorWiringError: DC-B4 — the resolved config says
            cleanup_after_analysis is True but the trigger-builder
            returned None. Fail-loud at construction time.
        RetentionConfigError: A config file is present but malformed.
            Propagated unchanged.
        AttributeError: ``resources`` is missing a required attribute
            (e.g., a future ProjectResources refactor drops one).
            Propagated unchanged so the misuse fails fast at boot.

    Precondition:
        Schemas for ``BehaviorFlagsRepository``, ``DirectivesRepository``,
        ``AnalysisRunsRepository``, and ``SessionReportsRepository``
        must already exist on ``resources.db_engine``. The factory does
        NOT call ``create_schema()`` — first-run callers should do so
        before invoking the factory (or use a registry that wires the
        analysis-side schemas eagerly). Documented in scar-B6-3.
    """
    config = RetentionConfig.load(home=home, project_id=project_id)
    on_analysis_complete = _build_cleanup_trigger(
        config=config,
        resources=resources,
    )

    # DC-B4 wiring-consistency assertion. If config opted in but the
    # builder produced None, fail loud — the operator's opt-in must
    # never be silently dropped.
    if config.cleanup_after_analysis and on_analysis_complete is None:
        raise OrchestratorWiringError(
            "RetentionConfig.cleanup_after_analysis is True but the "
            "factory produced on_analysis_complete=None — the "
            "operator's opt-in to eager raw_traces cleanup would be "
            "silently disabled. This indicates a factory wiring bug; "
            "fix _build_cleanup_trigger or the factory call path."
        )

    # Construct analysis-side repos from the shared db_engine. ProjectRegistry
    # only wires up events + raw_trace_store; the analysis surface is
    # built lazily here, mirroring the CLI's pattern in cli/cleanup.py
    # (task-B5). All four repos share the project's intelligence.db
    # engine.
    return Orchestrator(
        events_repo=resources.events_repository,
        behavior_flags_repo=BehaviorFlagsRepository(resources.db_engine),
        directives_repo=DirectivesRepository(resources.db_engine),
        analysis_runs_repo=AnalysisRunsRepository(resources.db_engine),
        session_reports_repo=SessionReportsRepository(resources.db_engine),
        agent=agent,
        on_analysis_complete=on_analysis_complete,
    )


__all__ = [
    "OrchestratorWiringError",
    "build_orchestrator",
]
