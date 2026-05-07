"""Segment-level behavior flag detector (GUR-102 task-3).

Pipeline per segment:
  1. build_segment_prompt(segment, metrics) -> prompt str
  2. agent.analyze_segments([prompt]) -> [SegmentAnalysis]
  3. Pre-insert validation: validate EVERY flag in result.flags against
     _guard rules (flag_type ∈ BehaviorFlagType, confidence ∈ {high,medium,low})
     BEFORE any insert (closes DC-2).
  4. Convert BehaviorFlagDraft → BehaviorFlag with new id, project_id,
     session_id, segment_index, intent_summary, created_at.
  5. behavior_flags_repo.insert_many(flags) — atomic.

DC-2 contract:
  If ANY flag in the batch fails pre-insert validation, the entire batch
  is rejected BEFORE insert_many is called. No partial persistence.

ID generation policy (deterministic — enables idempotent re-runs):
  id = sha256(session_id + "|" + str(segment_index) + "|"
              + sorted(event_ids).join(",") + "|" + flag_type.value
              + "|" + reason).hexdigest()[:32]

  Inputs: session_id, segment_index, ALL event_ids (sorted for determinism),
  flag_type value string, draft.reason (LLM-generated).

  Rationale: deterministic IDs allow safe re-runs of the pipeline on the
  same segment (e.g., after a partial failure) without creating duplicate
  rows. Using all event_ids (sorted) and reason prevents the prior collision
  case where two flags sharing the same flag_type and first event_id would
  collapse to the same ID.

  Idempotency guarantee: same prompt → same LLM → same reason and same
  event_ids → same hash → safe re-run with ON CONFLICT DO NOTHING.

intent_summary assignment:
  BehaviorFlagDraft does not carry intent_summary (it is an orchestrator
  concern, not an LLM output field). This function fills intent_summary
  from the human-readable description in FLAG_DEFINITIONS[flag_type].
  The flag definition is frozen per SD §5.5.1; if flag_type is unrecognized
  (after pre-insert validation passes), FLAG_DEFINITIONS.get() falls back
  to the empty string — that path is unreachable in practice.

Layer boundary note:
  analyze_segments is called with a single-element list per segment.
  The orchestrator may eventually batch multi-segment calls for concurrency
  — at that point, detect_segment_flags will remain single-segment (the
  batch boundary moves up to the orchestrator). Do not conflate this
  function with the batch layer.

Async contract:
  Sync callers are NOT supported. The GUR-103 implementation of
  AnalysisAgent is async-native; sync wrappers are GUR-103's concern.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from secondsight.analysis.agent import AnalysisAgent
from secondsight.analysis.prompts.behavior import build_segment_prompt
from secondsight.analysis.schemas import (
    FLAG_DEFINITIONS,
    VALID_CONFIDENCE,
    BehaviorFlag,
    BehaviorFlagDraft,
    BehaviorFlagType,
    SegmentData,
    SegmentMetrics,
)

if TYPE_CHECKING:
    # BehaviorFlagsRepository is only needed for type annotations.
    # The runtime import is deferred to avoid a circular import:
    #   analysis/__init__.py
    #     -> analysis/behavior.py
    #     -> storage/behavior_flags_repository.py
    #     -> analysis/schemas.py
    #     -> (analysis package partially initialized)
    # With `from __future__ import annotations`, all annotations are lazy
    # strings at runtime, so this TYPE_CHECKING-only import is sufficient.
    from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository

def _make_flag_id(
    session_id: str,
    segment_index: int,
    event_ids: list[str],
    flag_type_value: str,
    reason: str,
) -> str:
    """Deterministic 32-char hex ID for a behavior flag.

    Inputs are chosen to be stable per detection context:
    - session_id: anchors to the session being analyzed
    - segment_index: anchors to the segment within the session
    - event_ids: ALL event IDs (sorted for determinism), not just first —
      prevents collision when two flags share the same flag_type and first
      event_id but reference different overall event sets
    - flag_type_value: distinguishes multiple flags on the same event
    - reason: LLM-generated description, likely unique for distinct flags
      even when flag_type and event_ids overlap

    Idempotency guarantee: same prompt → same LLM → same reason and same
    event_ids → same hash. Idempotent re-runs are safe.

    Truncated to 32 chars (128-bit prefix of SHA-256) — collision probability
    is negligible for the expected cardinality (tens of flags per session).
    """
    event_ids_canonical = ",".join(sorted(event_ids))
    raw = f"{session_id}|{segment_index}|{event_ids_canonical}|{flag_type_value}|{reason}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def validate_draft_pre_insert(draft: BehaviorFlagDraft, index: int) -> None:
    """Validate one draft flag against _guard rules BEFORE insert.

    This mirrors BehaviorFlagsRepository._guard but operates on
    BehaviorFlagDraft (pre-promotion). It is called on every draft in the
    batch before ANY insert is attempted, closing DC-2.

    Raises ValueError with a descriptive message on any violation.

    Defense-in-depth rationale:
    The repository's _guard also validates on insert. Pre-insert validation
    here catches the case where model_construct() was used to build the
    draft (bypassing Pydantic), ensuring the entire batch is rejected early
    rather than mid-insert. If the two guards ever drift, the repository
    guard catches anything that slips through here — a regression in
    behavior.py will surface as a task-1 death-test failure, not a silent
    data corruption.
    """
    # event_ids non-empty check: BehaviorFlagDraft.event_ids is list[str] with
    # no min_length constraint; an empty list violates the LLM contract (the
    # prompt instructs the model to reference at least one event). An empty
    # event_ids also collapses the ID to a "no-event" literal anchor, which
    # causes silent ID collision for multiple same-type empty-event flags.
    if not draft.event_ids:
        raise ValueError(
            f"Flag[{index}].event_ids is empty — flags must reference at least one event"
        )

    # Confidence check: Literal["high","medium","low"] not enforced on
    # model_construct() results.
    if draft.confidence not in VALID_CONFIDENCE:
        raise ValueError(
            f"Flag[{index}].confidence={draft.confidence!r} must be one of "
            f"{sorted(VALID_CONFIDENCE)}"
        )

    # flag_type check: enum not enforced on model_construct() results.
    # Normalize via enum constructor — raises ValueError for unknown values.
    if not isinstance(draft.flag_type, BehaviorFlagType):
        try:
            BehaviorFlagType(draft.flag_type)
        except ValueError as exc:
            raise ValueError(
                f"Flag[{index}].flag_type={draft.flag_type!r} is not a valid BehaviorFlagType"
            ) from exc


def promote_draft(
    draft: BehaviorFlagDraft,
    *,
    session_id: str,
    project_id: str,
    segment_index: int,
    created_at: datetime,
) -> BehaviorFlag:
    """Promote a validated BehaviorFlagDraft → BehaviorFlag.

    Injects persistence fields: id, project_id, session_id,
    segment_index, intent_summary, created_at.

    Precondition: validate_draft_pre_insert has already verified this draft.
    The flag_type is guaranteed to be in BehaviorFlagType (either already an
    instance, or a valid string — normalize here for consistency).

    intent_summary: filled from FLAG_DEFINITIONS[flag_type].description,
    which is frozen per SD §5.5.1. The BehaviorFlagDraft does not carry
    intent_summary (it is not an LLM output field); the orchestrator layer
    supplies it here.
    """
    # Normalize flag_type to enum instance.
    flag_type: BehaviorFlagType = (
        draft.flag_type
        if isinstance(draft.flag_type, BehaviorFlagType)
        else BehaviorFlagType(draft.flag_type)
    )

    # Precondition: validate_draft_pre_insert guarantees event_ids is non-empty.
    flag_id = _make_flag_id(
        session_id,
        segment_index,
        list(draft.event_ids),
        flag_type.value,
        draft.reason,
    )

    # intent_summary from FLAG_DEFINITIONS; empty string if missing (unreachable
    # post-validation but kept defensive).
    defn = FLAG_DEFINITIONS.get(flag_type)
    intent_summary = defn["description"] if defn else ""

    return BehaviorFlag(
        id=flag_id,
        project_id=project_id,
        session_id=session_id,
        segment_index=segment_index,
        flag_type=flag_type,
        event_ids=list(draft.event_ids),
        intent_summary=intent_summary,
        reason=draft.reason,
        confidence=draft.confidence,
        created_at=created_at,
    )


async def detect_segment_flags(
    segment: SegmentData,
    metrics: SegmentMetrics,
    *,
    session_id: str,
    project_id: str,
    behavior_flags_repo: BehaviorFlagsRepository,
    agent: AnalysisAgent,
) -> int:
    """Detect behavior flags in one segment and persist them.

    Pipeline:
      1. build_segment_prompt(segment, metrics) -> prompt str
      2. agent.analyze_segments([prompt]) -> [SegmentAnalysis]
      3. Validate every flag in result.flags against _guard rules
         (flag_type ∈ BehaviorFlagType, confidence ∈ {high,medium,low})
         BEFORE any insert.
      4. Convert BehaviorFlagDraft → BehaviorFlag with new id, project_id,
         session_id, segment_index, intent_summary, created_at.
      5. behavior_flags_repo.insert_many(flags) — atomic.

    Returns: number of flags attempted (0 to len(result.flags)).
    Note: return value matches insert_many semantics — it is len(input),
    not the count of newly persisted rows (ON CONFLICT DO NOTHING may skip
    duplicates silently). Use get_session_flags to verify actual persistence.

    Raises:
      - AnalysisAgentError: agent call failed irrecoverably.
      - ValueError: pre-insert _guard validation rejected a flag (DC-2).

    On any raise, NO flags are persisted from this call (atomic guarantee
    enforced by pre-insert validation before insert_many is called).
    """
    prompt = build_segment_prompt(segment, metrics)

    # Step 2: call agent (may raise AnalysisAgentError — let it propagate).
    # Single-element list — orchestrator batches across segments at the next layer.
    results = await agent.analyze_segments([prompt])
    # analyze_segments with a single-element prompt list returns a single-element
    # result list. len(results) == 1 guaranteed by Protocol contract.
    result = results[0]

    drafts = result.flags

    # Short-circuit for empty flags — no validation, no insert needed.
    if not drafts:
        return 0

    # Step 3: Pre-insert validation (DC-2). Validate ENTIRE batch before any insert.
    # A single bad flag raises ValueError and aborts before insert_many is called.
    for i, draft in enumerate(drafts):
        validate_draft_pre_insert(draft, i)

    # Step 4: Promote drafts to BehaviorFlags with persistence fields.
    created_at = datetime.now(tz=timezone.utc)
    flags = [
        promote_draft(
            draft,
            session_id=session_id,
            project_id=project_id,
            segment_index=segment.segment_index,
            created_at=created_at,
        )
        for draft in drafts
    ]

    # Step 5: Persist. insert_many is atomic (single DB transaction).
    # NOTE for task-5 orchestrator caller: this is len(flags), the
    # attempted count. ON CONFLICT DO NOTHING on `id` may silently drop
    # duplicates — the actual persisted row count may be lower. The
    # orchestrator should rely on this return for "how many flags were
    # detected this run" reporting; for "rows confirmed in DB", query
    # behavior_flags_repo directly (e.g., get_session_flags or count_by_type).
    return behavior_flags_repo.insert_many(flags)


__all__ = ["detect_segment_flags", "validate_draft_pre_insert", "promote_draft"]
