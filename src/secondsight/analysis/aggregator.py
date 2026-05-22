"""Cross-session aggregator implementing SD §5.5.3 (GUR-102 task-4).

Steps:
  1 (automated): Group behavior_flags by flag_type across all sessions.
  2 (LLM, one call per non-empty group): Call agent.aggregate_flag_type(prompt)
     once per flag_type; receive AggregateOutput with discovered patterns.
  3 (automated): Merge all AggregatePattern instances, sort by occurrence_count
     DESC with deterministic tie-break, take top DEFAULT_CONVENTION_TOP_N=15,
     resolve/reuse lineage identity, then UPSERT to directives.

Death cases addressed here:
- DC-3: Step-3 top-N silent tie-truncation. Deterministic tie-break by
  (flag_type.value ASC, pattern_description ASC) makes re-runs converge.
- DC-5: Aggregator reads only what the repo returns; result.flags_read
  discloses this count (retention-purge disclosure surface).
- DC-6: Representative-session snapshots remain a diagnostic artifact only.
  Canonical directive identity is resolved separately and is not derived from
  `representative_sessions`.

Failure semantics (read carefully — task-5 orchestrator):

- **Step 2 (LLM calls) is all-or-nothing.** If any per-flag-type
  call raises AnalysisAgentError, no directives are upserted in this
  run. Retry is safe — the DB state is identical to the pre-call state.

- **Step 3 (DB UPSERTs) is per-row transactional, NOT atomic across
  the loop.** Each directive UPSERT is its own engine.begin()
  transaction. If the K-th UPSERT raises (DB lock timeout,
  _guard ValueError, IntegrityError, disk full), K-1 directives
  have been written and N-K have not. The caller receives an
  exception, not an AggregateProjectResult.

  Retry is **idempotent-safe** — the K-1 already-written rows
  converge under UPSERT(identity_key); the missing N-K will be
  written on retry. But the caller cannot distinguish Step 2 vs.
  Step 3 failure from the exception type alone. Inspect the DB
  state via DirectivesRepository.get_active_conventions() if you
  need to know.

Assumptions explicitly stated:
- BehaviorFlag.intent_summary maps to FlagSummary.segment_summary.
  (FlagSummary's field is segment_summary; BehaviorFlag's field is
  intent_summary. The adaptation is deliberate — see task-3 scar for
  rationale. The prompt builder is unconcerned with the source field name.)
- BehaviorFlagType enum has ≤ 7 members. Fan-out is O(flag_types), which
  is small and acceptable for all-or-nothing semantics.
- flags_read denominator is the total flags returned by the repo across all
  flag_types in this run. NOT a historical total.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final

from secondsight.analysis.agent import AnalysisAgent
from secondsight.analysis.prompts.aggregate import (
    AggregateOutput,
    AggregatePattern,
    FlagSummary,
    build_aggregate_prompt,
)
from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveRevision,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.config.schema import DirectiveLifecycleConfig
from secondsight.feedback.dedup import DedupVerdict, check_semantic_dedup
from secondsight.feedback.directive_identity import resolve_or_create_identity
from secondsight.feedback.directive_policy import (
    DirectiveLifecycleSignal,
    evaluate_directive_policy_with_config,
)
from secondsight.feedback.directive_revision import build_revision_candidate
from secondsight.feedback.lifecycle import validate_transition
from secondsight.feedback.lifecycle_automation import _enforce_capacity_ceiling
from secondsight.storage.directive_revisions_repository import DirectiveRevisionsRepository

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Avoid circular import: storage.behavior_flags_repository imports
    # analysis.schemas; analysis.__init__ imports aggregator (here).
    # Guarding under TYPE_CHECKING prevents the cycle at runtime.
    # At runtime, duck typing suffices — the Protocol-style interface
    # is not enforced structurally here; mypy/pyright catch it statically.
    from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
    from secondsight.storage.directives_repository import DirectivesRepository

# Hard-coded for v1. Configurable via analysis_config.toml deferred —
# see pre-thinking G2 for rationale.
# TODO(future): make configurable via analysis_config.toml
# convention_top_n key (SD §11 line 1392).
DEFAULT_CONVENTION_TOP_N: Final[int] = 15


def _pattern_description_matches(left: str, right: str) -> bool:
    left_tokens = set(re.sub(r"[^\w\s]", "", left.lower()).split())
    right_tokens = set(re.sub(r"[^\w\s]", "", right.lower()).split())
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.6


def compute_identity_key(
    project_id: str,
    flag_type: BehaviorFlagType,
    representative_sessions: Sequence[str],
) -> str:
    """Legacy deterministic pattern fingerprint helper.

    Lifecycle hygiene no longer uses representative-session hashes as the
    canonical directive identity. The helper remains public for tests and
    diagnostics that need a reproducible snapshot fingerprint.
    """
    sorted_sessions = sorted(representative_sessions)
    raw = f"{project_id}|{flag_type.value}|{','.join(sorted_sessions)}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class AggregateProjectResult:
    """Outcome of one aggregate_project_flags run.

    Fields:
        project_id: the project that was aggregated.
        calls_made: number of per-flag-type LLM calls in Step 2.
        flags_read: total flags returned by the repo in Step 1 (DC-5
            disclosure). Does NOT include flags purged by retention.
        patterns_emerged: total patterns produced across all Step-2 calls,
            before top-N truncation.
        directives_upserted: the number of directives written to the DB
            (≤ top_n ≤ DEFAULT_CONVENTION_TOP_N).
        aggregated_at: UTC timestamp when the run completed.
    """

    project_id: str
    calls_made: int
    flags_read: int
    patterns_emerged: int
    directives_upserted: int
    aggregated_at: datetime


async def aggregate_project_flags(
    project_id: str,
    *,
    behavior_flags_repo: BehaviorFlagsRepository,
    directives_repo: DirectivesRepository,
    agent: AnalysisAgent,
    top_n: int = DEFAULT_CONVENTION_TOP_N,
    capacity_ceiling: int = DEFAULT_CONVENTION_TOP_N,
    lifecycle_config: DirectiveLifecycleConfig | None = None,
) -> AggregateProjectResult:
    """Run Step 1 → Step 2 → Step 3 for one project.

    Args:
        project_id: the project to aggregate flags for.
        behavior_flags_repo: repository providing get_project_flags_by_type.
        directives_repo: repository providing upsert_with_identity_key.
        agent: LLM agent providing aggregate_flag_type.
        top_n: maximum number of directives to upsert. Must be >= 0.
            0 is a valid no-op (Step 2 still runs; no directives written).
            Negative values raise ValueError immediately — loud
            misconfiguration failure rather than a silent empty result.
            Default: DEFAULT_CONVENTION_TOP_N (15).
        capacity_ceiling: maximum number of active conventions retained after
            lifecycle policy and revision processing. Must be > 0.

    Raises:
        ValueError: if top_n < 0.
        ValueError: if capacity_ceiling <= 0.
        AnalysisAgentError: if any Step-2 LLM call fails (Step 2
            all-or-nothing — see module docstring Failure semantics).

    Step 1 — Group by flag_type:
        Iterates BehaviorFlagType in definition order (stable enum ordering).
        Empty flag_types are skipped — no LLM call for them.

        Field name adaptation: BehaviorFlag.intent_summary is used as
        FlagSummary.segment_summary. The FlagSummary model's field name is
        segment_summary (matching the SD §5.5.3 prompt's input field name);
        BehaviorFlag's corresponding field is intent_summary (set by the
        orchestrator from FLAG_DEFINITIONS[flag_type].description). This
        adaptation is documented here and in task-4-scar.yaml.

    Step 2 — Per-flag-type LLM:
        One await agent.aggregate_flag_type(prompt) per non-empty group.
        ALL calls are collected before Step 3 begins. If any call raises,
        the exception propagates immediately; no directives are upserted.

    Step 3 — Merge, sort, top-N, UPSERT:
        Sort key: (-occurrence_count, flag_type.value ASC, pattern_description ASC).
        The two tie-break fields are both ASC so repeated runs always pick
        the same subset at the top_n boundary (DC-3).

        frequency = occurrence_count / flags_read (local to this project run).
        If flags_read == 0 (empty project), frequency = 0.0.

        source_sessions stores the emerged pattern's representative_sessions
        (NOT the full set of contributing session_ids). They remain useful
        diagnostic context, but they do not define canonical identity.
    """
    if top_n < 0:
        raise ValueError(
            f"top_n must be >= 0, got {top_n!r}. "
            "Pass top_n=0 for an explicit no-op (no directives written)."
        )
    if capacity_ceiling <= 0:
        raise ValueError(
            f"capacity_ceiling must be > 0, got {capacity_ceiling!r}. "
            "Pass a positive per-project directive_lifecycle.capacity_ceiling."
        )
    effective_lifecycle_config = lifecycle_config or DirectiveLifecycleConfig(
        capacity_ceiling=capacity_ceiling
    )

    # ------------------------------------------------------------------ Step 1
    # Group flags by flag_type. Only non-empty groups proceed to Step 2.
    flags_by_type: dict[BehaviorFlagType, list[FlagSummary]] = {}
    flags_read_total = 0

    for flag_type in BehaviorFlagType:
        raw_flags: list[BehaviorFlag] = behavior_flags_repo.get_project_flags_by_type(
            project_id, flag_type
        )
        if not raw_flags:
            continue
        flags_read_total += len(raw_flags)
        # Adaptation: BehaviorFlag.intent_summary → FlagSummary.segment_summary.
        # See module docstring for rationale.
        flags_by_type[flag_type] = [
            FlagSummary(
                session_id=f.session_id,
                segment_summary=f.intent_summary,  # field name adaptation
                reason=f.reason,
            )
            for f in raw_flags
        ]

    # Short-circuit: no flags → no LLM calls, no directives.
    if not flags_by_type:
        return AggregateProjectResult(
            project_id=project_id,
            calls_made=0,
            flags_read=flags_read_total,
            patterns_emerged=0,
            directives_upserted=0,
            aggregated_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------ Step 2
    # Fan-out: one LLM call per non-empty flag_type group.
    # ALL calls must succeed before we proceed to Step 3 (all-or-nothing).
    all_patterns: list[tuple[BehaviorFlagType, AggregatePattern]] = []
    calls_made = 0

    for flag_type, summaries in flags_by_type.items():
        prompt = build_aggregate_prompt(flag_type, summaries)
        # May raise AnalysisAgentError — propagates immediately if so.
        # No try/except here; that is intentional: all-or-nothing.
        output: AggregateOutput = await agent.aggregate_flag_type(prompt)
        calls_made += 1
        for pattern in output.patterns:
            all_patterns.append((flag_type, pattern))

    patterns_emerged = len(all_patterns)

    # ------------------------------------------------------------------ Step 3
    # Sort: primary DESC on occurrence_count; ties broken by flag_type.value
    # ASC then pattern_description ASC — both deterministic and stable across
    # re-runs (DC-3).
    all_patterns.sort(
        key=lambda fp: (
            -fp[1].occurrence_count,  # primary DESC
            fp[0].value,  # tie-break 1: flag_type string ASC
            fp[1].pattern_description,  # tie-break 2: description ASC
        )
    )

    top = all_patterns[:top_n]
    now = datetime.now(timezone.utc)
    upserted = 0
    skipped_dedup = 0
    repromoted_identity_keys: set[str] = set()
    revisions_repo = DirectiveRevisionsRepository(directives_repo._db)
    revisions_repo.create_schema()

    # Identity reuse must see both pre-existing directives and directives
    # already written earlier in this same aggregation run.
    identity_scope_directives = directives_repo.list_for_identity_resolution(project_id)
    active_conventions = directives_repo.get_active_conventions(project_id)
    run_identity_descriptors: list[Directive] = []

    for flag_type, pattern in top:
        cached_directive = next(
            (
                d
                for d in run_identity_descriptors
                if d.source_flag_type == flag_type.value
                and _pattern_description_matches(pattern.pattern_description, d.instruction)
            ),
            None,
        )
        if cached_directive is not None:
            identity_key = cached_directive.identity_key
            matched_directive_id = cached_directive.id
            matched_status = cached_directive.status
        else:
            resolution = resolve_or_create_identity(
                project_id=project_id,
                flag_type=flag_type,
                pattern_description=pattern.pattern_description,
                candidate_instruction=pattern.convention,
                representative_sessions=list(pattern.representative_sessions),
                existing_directives=identity_scope_directives,
            )
            if resolution.is_unknown:
                _logger.warning(
                    "aggregator: identity resolution ambiguous for project_id=%r flag_type=%s pattern=%r reason=%s",
                    project_id,
                    flag_type.value,
                    pattern.pattern_description,
                    resolution.match_reason,
                )
                continue
            identity_key = resolution.identity_key
            matched_directive_id = resolution.matched_directive_id
            matched_status = resolution.matched_status
            if identity_key is None:
                _logger.warning(
                    "aggregator: identity resolution returned no lineage id for project_id=%r flag_type=%s pattern=%r",
                    project_id,
                    flag_type.value,
                    pattern.pattern_description,
                )
                continue

        # P3B-1: semantic dedup check before UPSERT.
        dedup_result = check_semantic_dedup(
            pattern.convention,
            active_conventions,
            exclude_identity_key=identity_key,
        )

        if dedup_result.verdict == DedupVerdict.SKIP:
            skipped_dedup += 1
            _logger.info(
                "aggregator: skipped duplicate convention for flag_type=%s "
                "(similarity=%.3f, matched=%r)",
                flag_type.value,
                dedup_result.similarity,
                dedup_result.matched_directive_id,
            )
            continue

        if dedup_result.verdict == DedupVerdict.SUPERSEDE and dedup_result.matched_directive_id:
            try:
                validate_transition(
                    DirectiveStatus.ACTIVE,
                    DirectiveStatus.SUPERSEDED,
                    directive_id=dedup_result.matched_directive_id,
                )
                directives_repo.update_status(
                    dedup_result.matched_directive_id,
                    DirectiveStatus.SUPERSEDED,
                )
                _logger.info(
                    "aggregator: superseded directive_id=%r with more "
                    "precise convention (similarity=%.3f)",
                    dedup_result.matched_directive_id,
                    dedup_result.similarity,
                )
                active_conventions = [
                    d for d in active_conventions if d.id != dedup_result.matched_directive_id
                ]
                identity_scope_directives = [
                    d
                    for d in identity_scope_directives
                    if d.id != dedup_result.matched_directive_id
                ]
            except Exception as exc:
                _logger.warning(
                    "aggregator: failed to supersede directive_id=%r: %s",
                    dedup_result.matched_directive_id,
                    exc,
                )

        if matched_directive_id is not None and matched_status is DirectiveStatus.OBSOLETE:
            try:
                validate_transition(
                    DirectiveStatus.OBSOLETE,
                    DirectiveStatus.ACTIVE,
                    directive_id=matched_directive_id,
                )
                directives_repo.update_status(
                    matched_directive_id,
                    DirectiveStatus.ACTIVE,
                )
            except Exception as exc:
                _logger.warning(
                    "aggregator: failed to reactivate obsolete directive_id=%r: %s",
                    matched_directive_id,
                    exc,
                )
                continue

        frequency = (
            float(pattern.occurrence_count) / flags_read_total if flags_read_total > 0 else 0.0
        )
        directive = Directive(
            id=matched_directive_id or str(uuid.uuid4()),
            project_id=project_id,
            type=DirectiveType.CONVENTION,
            status=DirectiveStatus.ACTIVE,
            instruction=pattern.convention,
            frequency=frequency,
            source_flag_type=flag_type.value,
            source_sessions=list(pattern.representative_sessions),
            identity_key=identity_key,
            created_at=now,
            updated_at=now,
        )
        directives_repo.upsert_with_identity_key(directive)
        stored = directives_repo.get_by_project_identity_key(project_id, identity_key)
        if stored is not None:
            repromoted_identity_keys.add(stored.identity_key)
            if any(d.identity_key == stored.identity_key for d in identity_scope_directives):
                identity_scope_directives = [
                    d for d in identity_scope_directives if d.identity_key != stored.identity_key
                ]
                identity_scope_directives.append(stored)
            run_identity_descriptors = [
                d for d in run_identity_descriptors if d.identity_key != stored.identity_key
            ]
            run_identity_descriptors.append(
                Directive(
                    id=stored.id,
                    project_id=stored.project_id,
                    type=stored.type,
                    status=stored.status,
                    instruction=pattern.pattern_description,
                    source_flag_type=flag_type.value,
                    source_sessions=list(pattern.representative_sessions),
                    identity_key=stored.identity_key,
                    created_at=stored.created_at,
                    updated_at=stored.updated_at,
                )
            )
        upserted += 1

    if skipped_dedup > 0:
        _logger.info(
            "aggregator: semantic dedup skipped %d duplicate convention(s) for project_id=%r",
            skipped_dedup,
            project_id,
        )

    seen_source_flag_types = {flag_type.value for flag_type, _ in all_patterns}
    for directive in directives_repo.list_for_identity_resolution(project_id):
        signal = DirectiveLifecycleSignal(
            directive=directive,
            now=now,
            same_identity_repromoted=directive.identity_key in repromoted_identity_keys,
            source_flag_seen=bool(directive.source_flag_type)
            and directive.source_flag_type in seen_source_flag_types,
        )
        decision = evaluate_directive_policy_with_config(signal, effective_lifecycle_config)
        if not decision.applied:
            continue
        directives_repo.update_policy_state(
            directive.id,
            status=decision.new_status,
            weight=decision.new_weight,
            miss_streak=decision.new_miss_streak,
            last_promoted_at=decision.last_promoted_at,
            last_source_flag_seen_at=decision.last_source_flag_seen_at,
        )
        refreshed = directives_repo.get_by_id(directive.id)
        if refreshed is None or not decision.should_revise:
            continue

        candidate = build_revision_candidate(refreshed)
        candidate_instruction = candidate.instruction if candidate is not None else None
        accepted = bool(candidate_instruction) and candidate_instruction != refreshed.instruction
        revision_row = DirectiveRevision(
            id=str(uuid.uuid4()),
            project_id=refreshed.project_id,
            directive_id=refreshed.id,
            identity_key=refreshed.identity_key,
            revision_index=revisions_repo.next_revision_index(refreshed.id),
            old_instruction=refreshed.instruction,
            new_instruction=candidate_instruction or refreshed.instruction,
            reason=decision.reason,
            accepted=accepted,
            review_note=(candidate.strategy if accepted and candidate is not None else "rejected"),
            created_at=now,
        )
        revisions_repo.append(revision_row)
        if accepted:
            directives_repo.apply_revision(
                refreshed.id,
                new_instruction=revision_row.new_instruction,
                revised_at=now,
            )

    _enforce_capacity_ceiling(
        project_id,
        directives_repo,
        ceiling=effective_lifecycle_config.capacity_ceiling,
    )

    return AggregateProjectResult(
        project_id=project_id,
        calls_made=calls_made,
        flags_read=flags_read_total,
        patterns_emerged=patterns_emerged,
        directives_upserted=upserted,
        aggregated_at=now,
    )


__all__ = [
    "DEFAULT_CONVENTION_TOP_N",
    "AggregateProjectResult",
    "aggregate_project_flags",
    "compute_identity_key",
]
