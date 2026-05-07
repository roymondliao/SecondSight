"""Analysis pipeline orchestrator (GUR-102 task-5).

Composes four prior layers into three callable entrypoints:
  - analyze_session: segment → detect flags → summarize → persist report
  - aggregate_project: delegate to aggregate_project_flags (task-4)
  - analyze_and_aggregate: chain + zero-flag short-circuit guard (DC-8)

Death cases closed here:
  DC-1: start_run BEFORE any pipeline work. A SIGKILL after start_run
        leaves an audit row at stage='pending' for the retry/inspection
        logic. Without this, a SIGKILL mid-pipeline leaves zero trace.
  DC-4: Idempotency gate — completed session raises SessionAlreadyAnalyzedError
        unless force=True. Prevents silent duplicate LLM calls on re-trigger.
  DC-7: Consumer-not-recoverer verifier. analyze_session raises
        SessionIncompleteError when the events table has zero rows for the
        session. The orchestrator does NOT attempt GUR-99 backfill —
        it names the upstream contract violation so the operator can fix the
        root cause rather than masking it here.
  DC-8: Zero-flag short-circuit with stale-conventions disclosure. When
        flags_attempted == 0, aggregate is skipped. If directives exist with
        updated_at older than STALE_DIRECTIVES_THRESHOLD_DAYS, a WARNING is
        emitted so the operator knows conventions may be based on stale data.

SegmentAnalysis collection for summary step (Decision log):
  detect_segment_flags (task-3) returns int — the count of flags attempted.
  It calls agent.analyze_segments internally; the SegmentAnalysis is consumed
  and discarded there. For the summary step (step 6), the orchestrator needs
  the SegmentAnalysis objects.

  Options considered:
    (a) Re-fetch BehaviorFlag rows + reconstruct SegmentAnalysis — lossy
        (reason/confidence/total_events/flagged_events lost on round-trip).
    (b) Refactor detect_segment_flags to return tuple[int, SegmentAnalysis]
        — cleanest semantics, but requires modifying task-3 files which are
        outside task-5's scope per the implementation plan.
    (c) Orchestrator calls agent.analyze_segments directly AND calls
        detect_segment_flags separately — doubles LLM calls per segment.
    (d) Orchestrator calls agent.analyze_segments once per segment, then
        manually runs the validate+promote+insert pipeline from task-3.
        This duplicates the promotion logic from behavior.py.

  CHOSEN: (d') Variant of (d): orchestrator calls agent.analyze_segments
  once, collects SegmentAnalysis, then calls a private helper that
  replicates the validate+promote+insert logic from behavior.py using the
  already-obtained SegmentAnalysis. This avoids a second LLM call (vs c),
  avoids lossy reconstruction (vs a), and avoids modifying task-3 files
  (vs b). The duplication cost: if behavior.py's logic changes (e.g., a
  new validation rule), the orchestrator's helper must also be updated.
  This risk is documented in the scar report.

  Rationale over (c): LLM calls are expensive. Doubling them per segment
  is unacceptable. (d') pays a code-duplication cost instead of a runtime
  cost.

Outer try/except rationale:
  Steps 4-8 are wrapped in a single try/except Exception at the pipeline
  boundary. This is the ONE place a catch-all is acceptable:
  - It ensures `record_failure(run_id, ...)` is always called before
    propagation, completing the audit trail.
  - The exception is re-raised immediately — no swallowing.
  - Without this, an unhandled exception in step 5 or 7 would leave
    the analysis_runs row at a non-terminal stage indefinitely, making
    count_recent_partial show it as "stuck" forever.

Filesystem backup path (SD §7.2 line 209):
  {SECONDSIGHT_HOME}/projects/{project_id}/sessions/{session_id}/session_report.json
  where SECONDSIGHT_HOME = ~/.secondsight (or SECONDSIGHT_HOME env override).
  If the file already exists from a prior run, it is overwritten (the DB
  UPSERT keys on session_id; the filesystem follows the DB).

Stale directives threshold:
  STALE_DIRECTIVES_THRESHOLD_DAYS = 30 (hard-coded).
  TODO: make configurable via analysis_config.toml (SD §11 line 1392),
  mirroring task-4's DEFAULT_CONVENTION_TOP_N pattern. Deferred per G2
  rationale (same config plumbing not yet implemented).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final

from secondsight.analysis.aggregator import AggregateProjectResult, aggregate_project_flags
from secondsight.analysis.agent import AnalysisAgent, AnalysisAgentError
from secondsight.analysis.behavior import promote_draft, validate_draft_pre_insert
from secondsight.analysis.metrics import compute_segment_metrics
from secondsight.analysis.prompts.behavior import build_segment_prompt
from secondsight.analysis.prompts.summary import build_summary_prompt
from secondsight.analysis.schemas import (
    AnalysisRunStage,
    BehaviorFlag,
    SessionReport,
    SegmentAnalysis,
)
from secondsight.analysis.segmenter import Segmenter

if TYPE_CHECKING:
    from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
    from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
    from secondsight.storage.directives_repository import DirectivesRepository
    from secondsight.storage.events_repository import EventsRepository
    from secondsight.storage.session_reports_repository import SessionReportsRepository

_logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware.

    SQLite stores datetimes without timezone info; rows returned by the
    repository may be naive. This function attaches UTC if the datetime
    is naive, or converts to UTC if it carries another timezone.

    Used for comparisons between DB-read timestamps and UTC-aware thresholds.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Hard-coded for v1.
# TODO: make configurable via analysis_config.toml (SD §11 line 1392).
STALE_DIRECTIVES_THRESHOLD_DAYS: Final[int] = 30


class SessionIncompleteError(Exception):
    """Raised when verifier finds the session is missing event rows
    or end-event marker. Consumer-not-recoverer principle (DC-7).

    The orchestrator does NOT attempt GUR-99 backfill — it names the
    upstream contract violation so the operator can fix the root cause.
    Callers (GUR-103) decide retry policy.
    """


class SessionAlreadyAnalyzedError(Exception):
    """Raised when analyze_session is called on a session whose latest
    analysis_runs row is at stage='summary_written' or 'aggregated'.

    Pass force=True to re-analyze (DC-4).
    """


@dataclass(frozen=True)
class AnalyzeSessionResult:
    """Outcome of one analyze_session run.

    flags_attempted: number of flags the LLM emitted and validated this run;
    does NOT equal newly-persisted DB rows when ON CONFLICT DO NOTHING skips
    duplicates on force-rerun. Use behavior_flags_repo.get_session_flags to
    verify actual persistence.
    """

    run_id: str
    session_id: str
    project_id: str
    stage: AnalysisRunStage
    flags_attempted: int
    report_id: str | None  # set when stage reaches 'summary_written'


@dataclass(frozen=True)
class AnalyzeAndAggregateResult:
    """Outcome of one analyze_and_aggregate call."""

    session: AnalyzeSessionResult
    aggregate: AggregateProjectResult | None  # None when short-circuited


class Orchestrator:
    """Composes segmenter + behavior detector + summary + aggregator.

    All public methods are async-first (await on agent calls).

    Thread safety: not guaranteed. Designed for single-session sequential
    invocation from GUR-103's session-end trigger.
    """

    def __init__(
        self,
        events_repo: EventsRepository,
        behavior_flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        analysis_runs_repo: AnalysisRunsRepository,
        session_reports_repo: SessionReportsRepository,
        agent: AnalysisAgent,
        *,
        segmenter: Segmenter | None = None,
    ) -> None:
        self._events_repo = events_repo
        self._behavior_flags_repo = behavior_flags_repo
        self._directives_repo = directives_repo
        self._analysis_runs_repo = analysis_runs_repo
        self._session_reports_repo = session_reports_repo
        self._agent = agent
        # Allow DI of a fake segmenter for testing; default to real Segmenter.
        self._segmenter: Segmenter = segmenter or Segmenter(events_repo)

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    async def analyze_session(
        self, session_id: str, *, force: bool = False
    ) -> AnalyzeSessionResult:
        """Run the full per-session analysis pipeline.

        Steps:
          1. Verify session is complete (DC-7).
          2. Check idempotency gate (DC-4).
          3. Insert analysis_runs row at stage='pending' (DC-1).
          4. Segment + advance to 'segmented'.
          5. Per-segment: call agent + validate + persist flags → 'behavior_done'.
          6. Collect SegmentAnalysis objects → build summary prompt.
          7. Summarize + persist SessionReport + write filesystem backup.
          8. Advance to 'summary_written'; return AnalyzeSessionResult.

        On any uncaught exception in steps 4-8, records stage='failed' and
        re-raises. The audit row is always completed (terminal state).

        Failure semantics:
          - If pipeline raises and record_failure succeeds: analysis_runs row at
            stage='failed' with error_message populated. Caller sees the original
            pipeline exception.
          - If pipeline raises AND record_failure also fails: analysis_runs row
            remains at whatever stage was last set, with completed_at NULL. The
            audit trail is incomplete in this rare case. Caller still sees the
            original pipeline exception. Detection: count_recent_partial query.
        """
        # Step 1: consumer-not-recoverer verifier (DC-7).
        # Also extracts project_id in the same DB call to avoid a second
        # get_session_events() call (self-iteration fix — see scar report).
        project_id = self._verify_session_complete_and_get_project_id(session_id)

        # Step 2: idempotency gate (DC-4).
        self._check_already_analyzed(session_id, force=force)

        # Step 3: insert audit row BEFORE any pipeline work (DC-1).
        run_id = self._analysis_runs_repo.start_run(project_id, session_id)

        # Steps 4-8: pipeline boundary — catch all exceptions for audit.
        flags_inserted = 0
        report_id: str | None = None
        try:
            # Step 4: segment.
            segments = self._segmenter.segment_session(session_id)
            self._analysis_runs_repo.advance_stage(run_id, "segmented")

            # Step 5: per-segment detect + collect SegmentAnalysis.
            segment_analyses: list[SegmentAnalysis] = []
            for segment in segments:
                metrics = compute_segment_metrics(segment)
                prompt = build_segment_prompt(segment, metrics)

                # Call agent once per segment — result used for both flag
                # persistence and summary prompt construction (see module
                # docstring for decision rationale).
                results = await self._agent.analyze_segments([prompt])
                # Protocol contract: len(out) must equal len(in).
                # Guard against a real GUR-103 implementation that returns
                # an empty list, which would produce an IndexError rather
                # than the more informative AnalysisAgentError.
                if len(results) != 1:
                    raise AnalysisAgentError(
                        f"AnalysisAgent.analyze_segments contract violation: "
                        f"expected 1 result, got {len(results)} for segment "
                        f"{segment.segment_index}"
                    )
                analysis = results[0]
                segment_analyses.append(analysis)

                # Persist flags using task-3's validate+promote logic.
                # This replicates behavior.py's internal pipeline without
                # the agent call (already done above). See module docstring.
                inserted = self._persist_segment_flags(
                    analysis,
                    segment_index=segment.segment_index,
                    session_id=session_id,
                    project_id=project_id,
                )
                flags_inserted += inserted

            self._analysis_runs_repo.advance_stage(
                run_id, "behavior_done", flags_inserted=flags_inserted
            )

            # Step 6: build summary prompt from collected SegmentAnalysis objects.
            summary_prompt = build_summary_prompt(session_id, project_id, segment_analyses)

            # Step 7a: call agent for summary.
            summary_output = await self._agent.summarize_session(summary_prompt)

            # Step 7b: construct and persist SessionReport.
            now = datetime.now(tz=timezone.utc)
            report_id = str(uuid.uuid4())
            report = SessionReport(
                id=report_id,
                session_id=session_id,
                project_id=project_id,
                analysis_run_id=run_id,
                headline=summary_output.headline,
                key_findings=summary_output.key_findings,
                body=summary_output.body,
                created_at=now,
                updated_at=now,
            )
            self._session_reports_repo.upsert(report)

            # Step 7c: write filesystem JSON backup (SD §7.2 line 209).
            self._write_filesystem_backup(report)

            # Step 8: advance to terminal stage.
            self._analysis_runs_repo.advance_stage(run_id, "summary_written")

        except Exception as exc:
            # Outer pipeline boundary catch-all (see module docstring rationale).
            # record_failure sets stage='failed' + completed_at = now().
            try:
                self._analysis_runs_repo.record_failure(run_id, str(exc))
            except Exception as inner_exc:
                # record_failure itself failed (e.g., DB connection lost).
                # Log and continue propagation of the original exception.
                _logger.error(
                    "record_failure failed for run_id=%r after pipeline exception: %s",
                    run_id,
                    inner_exc,
                )
            raise

        return AnalyzeSessionResult(
            run_id=run_id,
            session_id=session_id,
            project_id=project_id,
            stage=AnalysisRunStage.SUMMARY_WRITTEN,
            flags_attempted=flags_inserted,
            report_id=report_id,
        )

    async def aggregate_project(self, project_id: str) -> AggregateProjectResult:
        """Delegate to the task-4 aggregator.

        Passes all repository dependencies and the agent through. Thin
        wrapper — all logic lives in aggregate_project_flags.
        """
        return await aggregate_project_flags(
            project_id,
            behavior_flags_repo=self._behavior_flags_repo,
            directives_repo=self._directives_repo,
            agent=self._agent,
        )

    async def analyze_and_aggregate(
        self, session_id: str, *, force: bool = False
    ) -> AnalyzeAndAggregateResult:
        """Run analyze_session, then optionally aggregate_project.

        Zero-flag short-circuit (DC-8): if analyze_session produces zero
        flags, aggregate is skipped. This avoids an expensive LLM fan-out
        when there is no new signal to aggregate.

        Stale-conventions disclosure: if aggregate is skipped AND the
        project has active directives with updated_at older than
        STALE_DIRECTIVES_THRESHOLD_DAYS, a WARNING is emitted. This
        discloses that existing conventions may be based on stale data
        (the session produced no new flags to update them).
        """
        session_result = await self.analyze_session(session_id, force=force)

        if session_result.flags_attempted == 0:
            _logger.info(
                "aggregator skipped: zero flags inserted for session %s",
                session_id,
            )
            # DC-8 stale-conventions disclosure.
            self._log_if_directives_stale(session_result.project_id)
            return AnalyzeAndAggregateResult(session=session_result, aggregate=None)

        aggregate_result = await self.aggregate_project(session_result.project_id)
        return AnalyzeAndAggregateResult(session=session_result, aggregate=aggregate_result)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _verify_session_complete_and_get_project_id(self, session_id: str) -> str:
        """Raise SessionIncompleteError if session has zero events (DC-7).
        Returns project_id extracted from the first event row.

        Consumer-not-recoverer principle: this verifier checks a
        precondition it cannot fix. The error message names the upstream
        contract violation (GUR-99 event ingestion pipeline) so the operator
        knows where to look.

        This verifier does NOT check for a SESSION_END event because:
        1. The events_repo.get_session_events() returns all events; counting
           them is a sufficient proxy for "does a session exist at all".
        2. An absent SESSION_END event may be a valid edge case (e.g.,
           session still in progress at trigger time) — raising on that
           case would be a false positive. The caller (GUR-103) controls
           when this is called.

        Combined with project_id extraction to avoid a second DB read.
        (Self-iteration fix: original design called get_session_events() twice.)

        Silent failure this guard closes: if the events table is empty for
        this session_id (e.g., GUR-99 crashed before writing events), the
        segmenter would return an empty list, producing a vacuous analysis
        with 0 segments and 0 flags. The SessionReport would be written
        with an empty body, silently corrupting the dashboard.
        """
        events = self._events_repo.get_session_events(session_id)
        if not events:
            raise SessionIncompleteError(
                f"session_incomplete: no events found for session_id={session_id!r}. "
                f"consumer-not-recoverer principle (DC-7): the orchestrator does NOT "
                f"attempt GUR-99 backfill. Fix the upstream event ingestion pipeline "
                f"(GUR-99) before re-triggering analysis for this session."
            )
        return events[0].project_id

    def _check_already_analyzed(self, session_id: str, *, force: bool) -> None:
        """Raise SessionAlreadyAnalyzedError if latest run is terminal (DC-4).

        Reads the latest analysis_runs row for this session_id. If the row
        is at a completed terminal stage (summary_written or aggregated),
        raises unless force=True.

        Silent failure this guard closes: if the idempotency check is absent,
        a second call to analyze_session (e.g., from a duplicate session-end
        trigger in GUR-103) silently re-runs the full pipeline, wasting LLM
        cost and potentially overwriting a better report with an identical one.

        Thread safety: NOT safe for concurrent invocation on the same session_id.
        Two parallel calls can both pass this check before either calls start_run,
        producing duplicate runs and doubled LLM cost. v1 assumes GUR-103 invokes
        the orchestrator from a single trigger per session — see scar task-5
        `assumptions_made[race_condition]` for analysis.
        """
        if force:
            return

        latest_run = self._analysis_runs_repo.get_latest_for_session(session_id)
        if latest_run is None:
            return

        # Terminal non-failed stages: summary_written, aggregated.
        completed_terminal = {
            AnalysisRunStage.SUMMARY_WRITTEN.value,
            AnalysisRunStage.AGGREGATED.value,
        }
        if latest_run.stage.value in completed_terminal:
            raise SessionAlreadyAnalyzedError(
                f"session_already_analyzed: session_id={session_id!r} has already been "
                f"analyzed (latest run {latest_run.id!r} at stage={latest_run.stage.value!r}). "
                f"Pass force=True to re-analyze and overwrite the existing report."
            )

    def _persist_segment_flags(
        self,
        analysis: SegmentAnalysis,
        *,
        segment_index: int,
        session_id: str,
        project_id: str,
    ) -> int:
        """Validate, promote, and persist flags from a SegmentAnalysis.

        Replicates the validate+promote+insert steps from behavior.py
        (detect_segment_flags) but takes an already-obtained SegmentAnalysis
        rather than calling the agent again.

        This is the chosen approach (d') documented in the module docstring.

        Returns: number of flags attempted (len(flags), not newly-persisted
        count, matching behavior.py's return semantics).

        On validation failure: raises ValueError before any insert (DC-2,
        same atomicity guarantee as detect_segment_flags).
        """
        drafts = analysis.flags

        if not drafts:
            return 0

        # Pre-insert validation — entire batch before any insert (DC-2).
        for i, draft in enumerate(drafts):
            validate_draft_pre_insert(draft, i)

        # Promote drafts to BehaviorFlags with persistence fields.
        created_at = datetime.now(tz=timezone.utc)
        flags: list[BehaviorFlag] = [
            promote_draft(
                draft,
                session_id=session_id,
                project_id=project_id,
                segment_index=segment_index,
                created_at=created_at,
            )
            for draft in drafts
        ]

        # Persist atomically.
        return self._behavior_flags_repo.insert_many(flags)

    def _write_filesystem_backup(self, report: SessionReport) -> None:
        """Write session_report.json at SD §7.2 line 209 path.

        Path: {SECONDSIGHT_HOME}/projects/{project_id}/sessions/{session_id}/session_report.json

        SECONDSIGHT_HOME = Path.home() / '.secondsight' by default,
        or SECONDSIGHT_HOME env variable if set (CLI convention from _home.py).

        On collision with a prior run's file: overwrite unconditionally.
        The DB UPSERT keys on session_id; the filesystem follows the DB.

        Silent failure mode: if the filesystem write fails (disk full, permissions),
        the exception propagates to the outer pipeline boundary and causes
        stage='failed'. The DB record is the authoritative source; the
        filesystem is a secondary cache for tools that bypass the DB.
        """
        secondsight_home_str = os.environ.get("SECONDSIGHT_HOME", "")
        if secondsight_home_str:
            home = Path(secondsight_home_str)
        else:
            home = Path.home() / ".secondsight"

        backup_path = (
            home
            / "projects"
            / report.project_id
            / "sessions"
            / report.session_id
            / "session_report.json"
        )
        backup_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "id": report.id,
            "session_id": report.session_id,
            "project_id": report.project_id,
            "analysis_run_id": report.analysis_run_id,
            "headline": report.headline,
            "key_findings": report.key_findings,
            "body": report.body,
            "created_at": report.created_at.isoformat(),
            "updated_at": report.updated_at.isoformat(),
        }
        backup_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _logger.debug(
            "session_report backup written: session_id=%r path=%s",
            report.session_id,
            backup_path,
        )

    def _log_if_directives_stale(self, project_id: str) -> None:
        """Log a WARNING if any active directive for this project is stale.

        A directive is considered stale if its updated_at is older than
        STALE_DIRECTIVES_THRESHOLD_DAYS (30 days, hard-coded).

        This is informational only — does not block the short-circuit.
        DC-8: discloses that conventions may be based on data that has since
        been purged or superseded, since no new flags were generated to
        refresh them in this session run.
        """
        try:
            conventions = self._directives_repo.get_active_conventions(project_id)
        except Exception as exc:
            _logger.warning(
                "_log_if_directives_stale: failed to read conventions for "
                "project_id=%r — %s",
                project_id,
                exc,
            )
            return

        if not conventions:
            return

        threshold = datetime.now(tz=timezone.utc) - timedelta(
            days=STALE_DIRECTIVES_THRESHOLD_DAYS
        )
        # Normalize directive timestamps: SQLite may return naive datetimes.
        # Make all comparisons in UTC-aware timezone.
        stale = [
            d for d in conventions
            if _ensure_utc(d.updated_at) < threshold
        ]
        if stale:
            _logger.warning(
                "stale-conventions: project_id=%r has %d active convention(s) "
                "with updated_at older than %d days (threshold=%s). "
                "These conventions were not updated because the current session "
                "produced zero new flags. Consider whether they are still valid. "
                "Oldest updated_at: %s",
                project_id,
                len(stale),
                STALE_DIRECTIVES_THRESHOLD_DAYS,
                threshold.date(),
                _ensure_utc(min(d.updated_at for d in stale)).isoformat(),
            )


__all__ = [
    "AnalyzeAndAggregateResult",
    "AnalyzeSessionResult",
    "Orchestrator",
    "SessionAlreadyAnalyzedError",
    "SessionIncompleteError",
    "STALE_DIRECTIVES_THRESHOLD_DAYS",
]
