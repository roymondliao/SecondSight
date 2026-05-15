"""Shared analysis runtime builder for server and CLI paths.

This module centralizes the per-project analysis assembly that used to live
only in the CLI path. The server uses the same builder so event-driven
dispatch and timeout recovery share one canonical runtime shape.

Task 6 additions:
  - ModeAwareDispatch: wraps CLIAnalysisDispatcher or SDKAnalysisDispatcher
    based on config.general.mode. This is the ONLY place mode branching occurs.
    All callers (sweeper, manual analyze, session-end hook) go through
    ProjectAnalysisRuntime.trigger for the legacy orchestrator path, or through
    ProjectAnalysisRuntime.mode_aware_dispatch for the new mode-aware path.

  - ProjectAnalysisRuntime gains a mode_aware_dispatch field. The legacy
    trigger/orchestrator path is preserved for callers that already use it.

Task 8 corrections (CRITICAL FIX 1):
  - build_project_analysis_runtime() no longer calls _build_analysis_agent() or
    references cfg.general.mode. The mode-conditional that existed in Task 7's
    implementation violated the invariant below. It has been moved:
    ModeAwareDispatch._get_sdk_dispatcher() now lazily constructs
    SDKAnalysisDispatcher (which internally creates LLMRouter) on the first
    SDK dispatch call. CLI mode never triggers this code path.

Architecture invariant (NON-NEGOTIABLE):
  After Task 6, no module outside ModeAwareDispatch should reference
  config.general.mode or check mode == "cli" / mode == "sdk". Mode-awareness
  is centralized in ModeAwareDispatch.dispatch() and ModeAwareDispatch._get_*
  dispatcher methods. build_project_analysis_runtime() is mode-agnostic.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from secondsight.analysis.behavior import promote_draft, validate_draft_pre_insert
from secondsight.analysis.config import AnalysisConfig
from secondsight.analysis.factory import build_orchestrator
from secondsight.analysis.orchestrator import Orchestrator
from secondsight.analysis.output import AnalysisOutput
from secondsight.analysis.schemas import SessionReport
from secondsight.analysis.tools import AnalysisTools
from secondsight.config import load_project_config
from secondsight.config.loader import _resolve_provider_keys
from secondsight.config.schema import SecondSightConfig
from secondsight.sdk.agent import PydanticAIAnalysisAgent
from secondsight.sdk.model_selection import select_model
from secondsight.sdk.router import LLMRouter
from secondsight.sdk.trigger import LockRegistry, Trigger
from secondsight.state import SecondSightState
from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.session_reports_repository import SessionReportsRepository

if TYPE_CHECKING:
    from secondsight.api.registry import ProjectResources
    from secondsight.analysis.dispatcher import AnalysisDispatcher
    from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository


# ---------------------------------------------------------------------------
# ModeAwareDispatch — Task 6: the ONLY place mode branching occurs
# ---------------------------------------------------------------------------


class ModeAwareDispatch:
    """Mode-aware dispatch wrapper.

    The single place in the codebase that reads config.general.mode and
    routes to CLIAnalysisDispatcher or SDKAnalysisDispatcher. All callers
    are mode-agnostic — they call dispatch() without knowing which dispatcher
    is active.

    DC10 protection (two-layer):
      1. Per-session asyncio.Lock (fast path): prevents concurrent dispatches
         for the same session_id from both executing. The lock is the PRIMARY
         guard — it prevents redundant LLM API calls before they happen.
      2. DB UNIQUE constraint on analysis_outputs.session_id (safety net): catches
         edge cases like crash-recovery races across multiple processes sharing the
         same intelligence.db. INSERT OR IGNORE semantics: second write is silently
         dropped. The lock prevents the attempt; the DB constraint catches escapes.

    Args:
        config: The resolved SecondSightConfig. config.general.mode determines
            which dispatcher is instantiated (cli or sdk).
        state: SecondSightState or None. Required for CLI mode when
            default_agent="auto". Ignored for SDK mode.
        project_id: The project this dispatch belongs to. Required for
            AnalysisOutputsRepository.insert_or_ignore(output, project_id=...).
            One ModeAwareDispatch instance per project (matching ProjectRegistry's
            per-project materialization model).
        repository: Optional AnalysisOutputsRepository for persisting dispatch
            results. If None, dispatch() still works but results are not persisted
            (used in tests or during gradual migration).
        cli_dispatcher: Optional injected CLIAnalysisDispatcher. If None and
            mode=cli, one is constructed at first dispatch. Injected for tests.
        sdk_dispatcher: Optional injected SDKAnalysisDispatcher. If None and
            mode=sdk, one is constructed at first dispatch. Injected for tests.

    Silent failure conditions (see scar report):
        - If mode is neither "cli" nor "sdk", dispatch() returns a failure
          AnalysisOutput with reason="unknown_mode". This should not happen
          in production since the config loader validates mode, but is possible
          if config is bypassed.
        - Per-session locks are in-process only. Across processes (e.g., two
          server instances with shared intelligence.db), DC10 is enforced only
          by the DB UNIQUE constraint in analysis_outputs table.
        - If repository.insert_or_ignore() raises an unexpected exception, the
          dispatch result is returned to the caller but the row is not persisted.
          The exception is logged at ERROR level; no retry.
    """

    def __init__(
        self,
        config: SecondSightConfig,
        state: SecondSightState | None,
        *,
        project_id: str = "",
        project_root: Path | None = None,
        repository: "AnalysisOutputsRepository | None" = None,
        flags_repository: BehaviorFlagsRepository | None = None,
        reports_repository: SessionReportsRepository | None = None,
        events_repository: EventsRepository | None = None,
        cli_dispatcher: "AnalysisDispatcher | None" = None,
        sdk_dispatcher: "AnalysisDispatcher | None" = None,
    ) -> None:
        self._config = config
        self._state = state
        self._project_id = project_id
        # project_root: absolute path to the project directory.
        # Required for CLI mode (subprocess cwd). Set at construction time since
        # ModeAwareDispatch is per-project (one instance per project_id).
        # When None, dispatch() falls back to the project_id-derived path or
        # passes None to the CLI dispatcher (which raises ValueError for CLI mode).
        self._project_root = project_root
        self._repository = repository
        self._flags_repository = flags_repository
        self._reports_repository = reports_repository
        self._events_repository = events_repository
        self._cli_dispatcher = cli_dispatcher
        self._sdk_dispatcher = sdk_dispatcher

        # Per-session lock registry for DC10 (PRIMARY GUARD):
        # Prevents concurrent dispatch for the same session_id from both executing.
        # asyncio.Lock is lightweight and cooperative. The dict maps session_id →
        # asyncio.Lock. Locks are created on first access and never removed
        # (bounded by number of unique sessions seen in this process lifetime).
        # The DB UNIQUE constraint on analysis_outputs.session_id is the SAFETY NET
        # for cross-process races; the lock handles the common in-process case.
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _get_or_create_lock(self, session_id: str) -> asyncio.Lock:
        """Return the asyncio.Lock for session_id, creating it if needed.

        This method is called from within a coroutine (dispatch()), so
        cooperative scheduling guarantees that no other coroutine runs
        between the membership check and dict.__setitem__. The check-and-set
        is therefore atomic within asyncio's single-threaded event loop.
        """
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _get_cli_dispatcher(self) -> "AnalysisDispatcher":
        """Construct or return the injected CLIAnalysisDispatcher."""
        if self._cli_dispatcher is not None:
            return self._cli_dispatcher

        from secondsight.analysis.cli_dispatcher import CLIAnalysisDispatcher

        return CLIAnalysisDispatcher(
            config=self._config.analysis,
            state=self._state,
        )

    def _get_sdk_dispatcher(self) -> "AnalysisDispatcher":
        """Construct (lazily) or return the injected SDKAnalysisDispatcher.

        This is the ONLY place that constructs an SDKAnalysisDispatcher (and
        therefore the only place that builds LLMRouter with provider keys).
        Construction is lazy: occurs only on the first SDK dispatch call.

        Architecture invariant (runtime.py lines 8-21): mode branching lives
        HERE and in dispatch(). No caller outside ModeAwareDispatch may check
        config.general.mode. The lazy construction here (not in
        build_project_analysis_runtime) ensures:
          1. CLI mode never attempts LLMRouter construction (no key required).
          2. SDK construction is deferred until the first dispatch, not at
             server startup (fail-at-use, not fail-at-boot for key errors).
        """
        if self._sdk_dispatcher is not None:
            return self._sdk_dispatcher

        from secondsight.analysis.sdk_dispatcher import SDKAnalysisDispatcher

        resolved_keys = _resolve_provider_keys(self._config.providers)
        self._sdk_dispatcher = SDKAnalysisDispatcher(
            config=self._config.analysis,
            resolved_keys=resolved_keys,
        )
        return self._sdk_dispatcher

    def _materialize_dashboard_artifacts(
        self,
        output: AnalysisOutput,
        *,
        project_id: str,
        analysis_run_id: str,
    ) -> None:
        """Persist dashboard-facing analysis artifacts for successful dispatches."""
        if self._reports_repository is None and self._flags_repository is None:
            return

        created_at = datetime.now(tz=timezone.utc)
        flags = []
        for index, draft in enumerate(output.behavior_flags):
            validate_draft_pre_insert(draft, index)
            # Mode-aware dispatch outputs are session-level, so segment granularity
            # is unavailable here. Persist under segment_index=0 so dashboard APIs
            # can consume the flags without reviving the legacy orchestrator path.
            flags.append(
                promote_draft(
                    draft,
                    session_id=output.session_id,
                    project_id=project_id,
                    segment_index=0,
                    created_at=created_at,
                )
            )

        if self._reports_repository is not None:
            report = SessionReport(
                id=f"sr-{uuid.uuid4()}",
                project_id=project_id,
                session_id=output.session_id,
                analysis_run_id=analysis_run_id,
                headline=output.session_summary.headline,
                key_findings=list(output.session_summary.key_findings),
                body=output.session_summary.body,
                created_at=created_at,
                updated_at=created_at,
            )
            self._reports_repository.upsert(report)

        if flags and self._flags_repository is not None:
            self._flags_repository.insert_many(flags)

    def _load_session_payload(self, session_id: str, *, project_id: str) -> dict[str, Any]:
        """Build a prompt payload from stored session events.

        Mode-aware dispatch is often triggered with only a session_id. In that
        case, loading the events here avoids silently degrading the analysis
        prompt to bare `{}` even though the session exists in storage.
        """
        if self._events_repository is None:
            logger.warning(
                f"ModeAwareDispatch: no events_repository available for "
                f"session_id={session_id!r}; falling back to empty payload"
            )
            return {}

        events = self._events_repository.get_session_events(session_id)
        if not events:
            logger.warning(
                f"ModeAwareDispatch: no events found for session_id={session_id!r}; "
                f"building empty events payload"
            )
            return {
                "session_id": session_id,
                "project_id": project_id,
                "events": [],
            }

        serialized_events: list[dict[str, Any]] = []
        first_prompt_text: str | None = None
        for event in events:
            event_ts = event.timestamp
            if event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=timezone.utc)
            else:
                event_ts = event_ts.astimezone(timezone.utc)
            event_payload: dict[str, Any] = {
                "event_id": event.id,
                "type": event.event_type.value,
                "timestamp": event_ts.isoformat(),
                "sequence_number": event.sequence_number,
                "segment_index": event.segment_index,
            }
            if event.sub_agent_id is not None:
                event_payload["sub_agent_id"] = event.sub_agent_id
            if event.depth:
                event_payload["depth"] = event.depth
            if event.duration_ms is not None:
                event_payload["duration_ms"] = event.duration_ms
            if event.token_count is not None:
                event_payload["token_count"] = event.token_count
            for key, value in event.data.items():
                if key not in event_payload:
                    event_payload[key] = value
            serialized_events.append(event_payload)

            if first_prompt_text is None:
                action_metadata = event.data.get("action_metadata")
                if isinstance(action_metadata, dict):
                    prompt_text = action_metadata.get("prompt_text")
                    if isinstance(prompt_text, str) and prompt_text:
                        first_prompt_text = prompt_text

        effective_project_id = events[0].project_id if events else project_id
        return {
            "session_id": session_id,
            "project_id": effective_project_id,
            "user_prompt": first_prompt_text,
            "events": serialized_events,
            "supplementary_metrics": {
                "total_events": len(serialized_events),
                "tool_calls": sum(
                    1 for event in events if event.event_type.value == "tool_use_start"
                ),
                "segment_count": len({event.segment_index for event in events}),
            },
        }

    async def dispatch(
        self,
        session_id: str,
        session_payload: dict[str, Any] | None = None,
        *,
        project_root: Path | None = None,
        project_id: str | None = None,
    ) -> AnalysisOutput:
        """Dispatch analysis for session_id via the configured mode.

        Mode selection lives HERE and nowhere else. Callers are mode-agnostic.

        DC10 (two-layer deduplication):
          Layer 1 — asyncio.Lock (PRIMARY GUARD): prevents concurrent dispatches
            for the same session_id from both executing. Fast path: no LLM API
            call is attempted. Returns no-op AnalysisOutput immediately.
          Layer 2 — DB UNIQUE constraint (SAFETY NET): if insert_or_ignore() is
            called for an already-existing session_id (e.g., cross-process race),
            the second insert is silently dropped by INSERT OR IGNORE semantics.

        After a successful dispatch, results are persisted via
        self._repository.insert_or_ignore(). If repository is None (test mode),
        persistence is skipped with a debug log.

        Args:
            session_id: Session to analyze.
            session_payload: Data dict for the prompt/request. Defaults to empty
                dict if None (caller-friendly for tests).
            project_root: Absolute path to project root. Required for CLI mode;
                ignored by SDK mode.
            project_id: Override project_id for this dispatch. If None, uses
                self._project_id (set at construction time). Callers like Trigger
                that pass project_id at call time use this parameter.

        Returns:
            AnalysisOutput. Never raises (exception-free contract matching
            the AnalysisDispatcher Protocol).
            On concurrent duplicate: returns AnalysisOutput with status='failure'
            and reason='dispatch_in_progress' (the first dispatch is still running).
            The no-op result is NOT persisted to analysis_outputs.
        """
        # Resolve the project_id for this dispatch: prefer the call-time override,
        # then fall back to the construction-time project_id.
        effective_project_id = project_id if project_id is not None else self._project_id

        if session_payload is None:
            session_payload = self._load_session_payload(
                session_id,
                project_id=effective_project_id,
            )

        # Resolve project_root: prefer call-time argument, then fall back to
        # the construction-time project_root (set for the per-project instance).
        effective_project_root = project_root if project_root is not None else self._project_root

        lock = self._get_or_create_lock(session_id)

        # Check if lock is already held — DC10 Layer 1 (PRIMARY GUARD).
        # lock.locked() returns True if another coroutine holds the lock.
        # Within asyncio's single-threaded cooperative scheduler, no other
        # coroutine can run between this check and the lock acquisition below
        # (no await between them), so the check is atomically stable.
        already_in_progress = lock.locked()
        if already_in_progress:
            logger.warning(
                f"ModeAwareDispatch: concurrent dispatch attempted for "
                f"session_id={session_id!r}. Returning no-op (DC10)."
            )
            # No-op result is NOT persisted to analysis_outputs — only successful
            # or real-failure dispatches are written. The no-op is a transient
            # signal to the caller (lock was held), not a durable output.
            return AnalysisOutput.model_validate(
                {
                    "schema_version": "1.0",
                    "session_id": session_id,
                    "status": "failure",
                    "behavior_flags": [],
                    "session_summary": {
                        "headline": "Dispatch skipped: already in progress",
                        "key_findings": [],
                        "body": (
                            f"Concurrent dispatch for session_id={session_id!r} "
                            f"was skipped. Another dispatch is in progress (DC10)."
                        ),
                    },
                    "dispatched_via": "cli" if self._config.general.mode == "cli" else "sdk",
                    "cli_agent": "unknown" if self._config.general.mode == "cli" else None,
                    "primary_model": None
                    if self._config.general.mode == "cli"
                    else self._config.analysis.sdk.primary_model,
                    "fallback_used": False,
                    "retry_count": 0,
                    "error_details": {"reason": "dispatch_in_progress"},
                }
            )

        async with lock:
            mode = self._config.general.mode
            logger.debug(
                f"ModeAwareDispatch: dispatching session_id={session_id!r} via mode={mode!r}"
            )

            if mode == "cli":
                dispatcher = self._get_cli_dispatcher()
                # Resolve effective agent for the log: prefer explicit default_agent,
                # fall back to state.init_agent when default_agent="auto".
                _default_agent = self._config.analysis.cli.default_agent
                if _default_agent == "auto" and self._state is not None:
                    _effective_agent = self._state.init_agent
                else:
                    _effective_agent = _default_agent
                logger.info(
                    f"dispatch start: session_id={session_id!r} mode={mode!r} "
                    f"agent={_effective_agent!r} primary_model=None"
                )
            elif mode == "sdk":
                dispatcher = self._get_sdk_dispatcher()
                _primary_model = self._config.analysis.sdk.primary_model
                logger.info(
                    f"dispatch start: session_id={session_id!r} mode={mode!r} "
                    f"agent=None primary_model={_primary_model!r}"
                )
            else:
                # Unknown mode — defensive path (loader validates, but in case bypassed)
                logger.error(
                    f"ModeAwareDispatch: unknown mode={mode!r} for session_id={session_id!r}. "
                    f"Valid modes: 'cli', 'sdk'. Returning failure output."
                )
                return AnalysisOutput.model_validate(
                    {
                        "schema_version": "1.0",
                        "session_id": session_id,
                        "status": "failure",
                        "behavior_flags": [],
                        "session_summary": {
                            "headline": "Dispatch failed: unknown mode",
                            "key_findings": [],
                            "body": f"config.general.mode={mode!r} is not valid.",
                        },
                        "dispatched_via": "sdk",  # default for failure shape
                        "cli_agent": None,
                        "primary_model": "unknown",
                        "fallback_used": False,
                        "retry_count": 0,
                        "error_details": {"reason": "unknown_mode", "mode": mode},
                    }
                )

            output = await dispatcher.dispatch(
                session_id,
                session_payload,
                project_root=effective_project_root,
            )

        # Persist the output to analysis_outputs (DC10 Layer 2: SAFETY NET).
        # Called OUTSIDE the lock to minimize lock hold time. The DB UNIQUE
        # constraint on session_id handles any cross-process race that escapes
        # the in-process lock.
        persisted_row_id: str | None = None
        if self._repository is not None:
            try:
                persisted_row_id = self._repository.insert_or_ignore(
                    output,
                    project_id=effective_project_id,
                )
                logger.debug(
                    f"ModeAwareDispatch: persisted output for "
                    f"session_id={session_id!r} project_id={effective_project_id!r}"
                )
            except Exception as exc:
                # Persistence failure must not lose the dispatch result.
                # Log at ERROR so the operator can investigate, but return the
                # output regardless (the LLM analysis already completed).
                logger.error(
                    f"ModeAwareDispatch: failed to persist output for "
                    f"session_id={session_id!r}: {type(exc).__name__}: {exc}"
                )
        else:
            logger.debug(
                f"ModeAwareDispatch: repository=None, skipping persistence for "
                f"session_id={session_id!r} (test mode or unconfigured)"
            )

        if output.status == "success":
            try:
                self._materialize_dashboard_artifacts(
                    output,
                    project_id=effective_project_id,
                    analysis_run_id=persisted_row_id or f"mode-aware-{session_id}",
                )
            except Exception as exc:
                logger.error(
                    f"ModeAwareDispatch: failed to materialize dashboard artifacts for "
                    f"session_id={session_id!r}: {type(exc).__name__}: {exc}"
                )

        return output


@dataclass(frozen=True)
class ProjectAnalysisRuntime:
    """Per-project analysis-side runtime owned by the server/CLI caller.

    Task 6 adds mode_aware_dispatch: the entry point for the new mode-aware
    dispatch path. The legacy trigger/orchestrator path is preserved for
    callers that already use it (sweeper, session-end hook via trigger).

    Fields:
        analysis_runs_repository: Audit trail for pipeline stage transitions.
        behavior_flags_repository: Per-session behavior flags.
        directives_repository: Project-level analysis directives.
        session_reports_repository: Structured session summaries.
        orchestrator: Legacy SDK orchestrator pipeline.
        trigger: Trigger layer (session-end hook + sweeper dispatch).
        mode_aware_dispatch: Task 6 mode-aware dispatcher (CLI or SDK).
    """

    analysis_runs_repository: AnalysisRunsRepository
    behavior_flags_repository: BehaviorFlagsRepository
    directives_repository: DirectivesRepository
    session_reports_repository: SessionReportsRepository
    orchestrator: Orchestrator
    trigger: Trigger
    mode_aware_dispatch: ModeAwareDispatch


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
    """Build the analysis agent chain for one project.

    NOTE (ship-manifest known debt, 2026-05-15): This function is intentionally
    preserved as a patchable sentinel for architecture-guard death tests in
    tests/analysis/test_runtime_wiring_death.py (test_dt_build_runtime_*_mode_
    never_calls_build_analysis_agent). Those tests `patch(...runtime._build_analysis_agent)`
    and assert the mock is NEVER called — proving the architecture invariant
    that build_project_analysis_runtime() does not eagerly construct an SDK
    agent (lazy construction lives in ModeAwareDispatch._get_sdk_dispatcher).

    Production call graph: NO live caller. _get_sdk_dispatcher inlines its
    own SDKAnalysisDispatcher construction directly (lines 197-204).

    Removal blocked on: migrating the death tests to grep-based architectural
    checks. Tracked in iteration-log.yaml round-1 residual_risk.
    """
    project_dir = Path(secondsight_home) / "projects" / project_id
    project_config_path = project_dir / "config.toml"

    analysis_config = AnalysisConfig.load(config_path=project_config_path)
    cfg = load_project_config(home=Path(secondsight_home), project_id=project_id)

    # select_model() uses structural typing: expects project_config.analysis.model
    # and global_config.analysis.{default_agent, models.*}. SecondSightConfig does NOT
    # match this shape directly (cfg.project_analysis.model, not cfg.analysis.model).
    # SimpleNamespace wrappers remap to the expected shape:
    #   project_config = SimpleNamespace(analysis=cfg.project_analysis)
    #   global_config  = SimpleNamespace(analysis=<GlobalAnalysisConfig-compatible>)
    #
    # analysis-mode-toggle task-1: cfg.analysis is now AnalysisConfig (new aggregate).
    # select_model() still expects the GlobalAnalysisConfig shape (default_agent, models).
    # Bridge: use cfg.analysis_global (GlobalAnalysisConfig preserved for this path).
    # Task 6 will replace select_model() with mode-aware dispatch that reads
    # cfg.analysis.cli.default_agent and cfg.analysis.sdk.primary_model directly.
    # If SecondSightConfig field names change, these wrappers will silently break.
    # The death tests in tests/config/test_runtime_wiring.py catch this regression.
    primary, fallbacks = select_model(
        project_id=project_id,
        project_config=SimpleNamespace(analysis=cfg.project_analysis),
        global_config=SimpleNamespace(analysis=cfg.analysis_global),
        events_repo=events_repository,
    )

    # Task 5: resolve provider keys from config (Decision E1 / DC8).
    # Loaded ONCE here — mid-flight env mutations have no effect (cache-once).
    resolved_keys = _resolve_provider_keys(cfg.providers)

    router = LLMRouter(primary=primary, fallbacks=fallbacks, resolved_keys=resolved_keys)
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
    """Build and return the shared per-project analysis runtime.

    Task 6: also constructs ModeAwareDispatch, wired to the project's config
    and state. The config determines CLI vs SDK mode; state is needed for
    CLI mode when default_agent="auto".
    """
    runs_repo = AnalysisRunsRepository(db_engine)
    flags_repo = BehaviorFlagsRepository(db_engine)
    directives_repo = DirectivesRepository(db_engine)
    reports_repo = SessionReportsRepository(db_engine)

    runs_repo.create_schema()
    flags_repo.create_schema()
    directives_repo.create_schema()
    reports_repo.create_schema()

    # Load project config — used by ModeAwareDispatch to determine mode at dispatch time.
    # NOTE: This function reads ~/.secondsight/state.json (below) and project config
    # from disk on every call without declaring this side effect in its signature.
    # This is a known coupling violation documented in IMPORTANT FIX 10 of
    # task-6-scar.yaml. Callers must not assume isolation.
    cfg = load_project_config(home=Path(secondsight_home), project_id=project_id)

    # The legacy Orchestrator is constructed with agent=None for ALL modes.
    # ModeAwareDispatch now owns all dispatcher construction (CRITICAL FIX 1 — Task 8):
    #   - CLI mode: ModeAwareDispatch._get_cli_dispatcher() builds CLIAnalysisDispatcher
    #     lazily on first CLI dispatch call. No LLMRouter, no API keys needed.
    #   - SDK mode: ModeAwareDispatch._get_sdk_dispatcher() builds SDKAnalysisDispatcher
    #     (including LLMRouter construction) lazily on first SDK dispatch call.
    #
    # This function NO LONGER references cfg.general.mode or calls _build_analysis_agent.
    # All mode-conditional logic is centralized in ModeAwareDispatch per the
    # Architecture invariant (runtime.py module docstring, lines 8-21).
    #
    # The agent=None here is safe: Trigger.dispatch() routes ONLY through
    # ModeAwareDispatch when mode_aware_dispatch is wired, bypassing
    # Orchestrator.analyze_and_aggregate() entirely.
    agent = cast("PydanticAIAnalysisAgent", None)  # type: ignore[arg-type]

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

    # Load state from the global secondsight home (not per-project)
    state_path = Path(secondsight_home) / "state.json"
    state: SecondSightState | None = None
    try:
        state = SecondSightState.load(state_path)
    except Exception as exc:
        logger.warning(
            f"build_project_analysis_runtime: could not load state.json "
            f"from {state_path!r}: {exc}. ModeAwareDispatch will use state=None."
        )

    # Build AnalysisOutputsRepository and create its schema.
    # This table is separate from analysis_runs (audit trail for orchestrator pipeline).
    # analysis_outputs stores ModeAwareDispatch results; analysis_runs stores
    # SDK orchestrator stage transitions.
    outputs_repo = AnalysisOutputsRepository(db_engine)
    outputs_repo.create_schema()

    # Derive project_root from secondsight_home + project_id.
    # This is the canonical per-project directory used by CLI dispatcher
    # as the subprocess cwd.
    project_root_path = Path(secondsight_home) / "projects" / project_id

    mode_aware_dispatch = ModeAwareDispatch(
        config=cfg,
        state=state,
        project_id=project_id,
        project_root=project_root_path,
        repository=outputs_repo,
        flags_repository=flags_repo,
        reports_repository=reports_repo,
        events_repository=events_repository,
    )

    trigger = Trigger(
        orchestrator=orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repository,
        lock_registry=LockRegistry(),
        mode_aware_dispatch=mode_aware_dispatch,
    )

    return ProjectAnalysisRuntime(
        analysis_runs_repository=runs_repo,
        behavior_flags_repository=flags_repo,
        directives_repository=directives_repo,
        session_reports_repository=reports_repo,
        orchestrator=orchestrator,
        trigger=trigger,
        mode_aware_dispatch=mode_aware_dispatch,
    )


__all__ = [
    "ModeAwareDispatch",
    "ProjectAnalysisRuntime",
    "build_project_analysis_runtime",
]
