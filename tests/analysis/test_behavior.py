"""Death + happy-path tests for detect_segment_flags (GUR-102 task-3).

Death tests come FIRST. Each death test names the silent failure mode it closes.

Death test inventory:
- DT-3.1: DC-2 — validation before insert atomicity. Bad flag #2 of 3 raises
          ValueError BEFORE insert_many is called; DB is unchanged.
- DT-3.2: Agent failure does not partial-write. AnalysisAgentError propagates;
          DB unchanged.
- DT-3.3: Empty SegmentAnalysis (zero flags). Returns 0 cleanly; insert_many
          is not called (or called with empty list — both are valid no-ops).
- DT-3.4: Confidence guard at pre-insert. confidence='unknown' raises ValueError
          before insert.
- DT-3.5: Empty event_ids raises ValueError before insert. A flag with event_ids=[]
          violates the LLM contract and would cause silent ID collision — must be
          rejected before any insert.

Happy-path tests:
- HP-3.A: Single-segment, 2 valid flags. Returns 2; flags retrievable from DB
          with correct flag_type, segment_index, confidence.
- HP-3.B: Idempotent re-call. Second call with same inputs inserts 0 new rows
          (ON CONFLICT DO NOTHING on deterministic IDs). Both calls return 2.

Assumptions:
- pytest-asyncio with @pytest.mark.asyncio on each async test.
- BehaviorFlagsRepository.insert_many returns len(input) always (not new-rows count).
- detect_segment_flags generates DETERMINISTIC IDs: sha256 of
  (session_id, segment_index, sorted(event_ids), flag_type.value, reason) — enabling HP-3.B.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.analysis.schemas import (
    BehaviorFlagDraft,
    BehaviorFlagType,
    SegmentAnalysis,
    SegmentData,
    SegmentMetrics,
)
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.db_engine import DBEngine
from tests.analysis._fake_agent import FakeAnalysisAgent

# --- import the function under test ---
# This import will FAIL until behavior.py is implemented. That is expected
# during the red phase of death tests.
from secondsight.analysis.behavior import detect_segment_flags


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[BehaviorFlagsRepository]:
    eng = DBEngine(tmp_path / "intel.db")
    r = BehaviorFlagsRepository(eng)
    r.create_schema()
    try:
        yield r
    finally:
        eng.dispose()


def _minimal_segment(
    segment_index: int = 0,
    session_id: str = "sess-test",
    project_id: str = "proj-test",
) -> SegmentData:
    """Minimal valid SegmentData for tests.

    Note: event_ids for BehaviorFlagDraft come from the FakeAnalysisAgent
    output, NOT from SegmentData.events. SegmentData.events is a list of
    ToolUseSpan/dict objects; flag event_ids are identifiers within those
    events. Tests that need specific event_ids should set them on
    BehaviorFlagDraft directly via _make_valid_draft(event_ids=[...]).
    """
    return SegmentData(
        segment_index=segment_index,
        user_prompt={"type": "user_prompt", "text": "Fix the bug"},
        events=[],
        session_id=session_id,
        project_id=project_id,
    )


def _minimal_metrics() -> SegmentMetrics:
    return SegmentMetrics(
        total_tokens=100,
        unique_files=2,
        duration=1.5,
        error_count=0,
    )


def _make_valid_draft(
    flag_type: BehaviorFlagType = BehaviorFlagType.UNNECESSARY_READ,
    event_ids: list[str] | None = None,
    confidence: str = "high",
) -> BehaviorFlagDraft:
    """Construct a valid BehaviorFlagDraft through Pydantic (validated)."""
    return BehaviorFlagDraft(
        flag_type=flag_type,
        event_ids=event_ids or ["evt-001"],
        reason="extraneous read",
        confidence=confidence,  # type: ignore[arg-type]
    )


def _make_segment_analysis_with_drafts(
    drafts: list[BehaviorFlagDraft],
) -> SegmentAnalysis:
    return SegmentAnalysis(
        segment_summary="Test segment",
        flags=drafts,
        total_events=10,
        flagged_events=len(drafts),
    )


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    @pytest.mark.asyncio
    async def test_dt_3_1_validation_before_insert_atomicity_dc2(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """DT-3.1 (= DC-2) — Bad flag #2 of 3 raises ValueError BEFORE insert_many.

        Silent failure this closes: if detect_segment_flags called
        insert_many without pre-validating, flag #1 would be persisted
        before flag #2 failed the _guard check inside insert_many, leaving
        orphan rows. After this death test: the entire batch must be
        validated before any insert is attempted.

        Strategy: use BehaviorFlagDraft.model_construct() to build a draft
        with an invalid flag_type (bypasses Pydantic). The fake returns a
        SegmentAnalysis built with model_construct on the draft list, also
        bypassing validation. The behavior detector must validate BEFORE
        calling insert_many.

        Post-condition: count_by_type for the project returns empty (no rows).
        """
        # Build 3 drafts: flag 1 valid, flag 2 INVALID (bad flag_type via
        # model_construct), flag 3 valid.
        valid_draft_1 = _make_valid_draft(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["evt-001"],
        )
        # model_construct bypasses Pydantic — simulates how a LLM response
        # could smuggle an invalid flag_type that survives JSON parsing if
        # the parser used model_construct instead of full Pydantic validation.
        bad_draft = BehaviorFlagDraft.model_construct(
            flag_type="totally_bogus_flag_type",  # NOT in BehaviorFlagType
            event_ids=["evt-002"],
            reason="bad flag from LLM",
            confidence="high",
        )
        valid_draft_3 = _make_valid_draft(
            flag_type=BehaviorFlagType.MISSED_SHORTCUT,
            event_ids=["evt-003"],
        )

        # Build SegmentAnalysis bypassing Pydantic so the bad draft survives.
        bad_analysis = SegmentAnalysis.model_construct(
            segment_summary="Three flags, one bad",
            flags=[valid_draft_1, bad_draft, valid_draft_3],
            total_events=10,
            flagged_events=3,
        )

        segment = _minimal_segment(session_id="sess-dt31", project_id="proj-dt31")
        metrics = _minimal_metrics()
        fake = FakeAnalysisAgent(segment_outputs=[bad_analysis])

        # The function MUST raise before persisting anything.
        with pytest.raises(ValueError, match="bogus|flag_type|invalid|BehaviorFlagType"):
            await detect_segment_flags(
                segment,
                metrics,
                session_id="sess-dt31",
                project_id="proj-dt31",
                behavior_flags_repo=repo,
                agent=fake,
            )

        # Post-condition: DB must be empty for this project.
        counts = repo.count_by_type("proj-dt31")
        assert counts == {}, (
            f"Expected no rows after ValueError, but found: {counts}. "
            "DC-2 violated: partial batch committed before validation failure."
        )

    @pytest.mark.asyncio
    async def test_dt_3_2_agent_failure_does_not_partial_write(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """DT-3.2 — AnalysisAgentError propagates; behavior_flags table unchanged.

        Silent failure this closes: if behavior.py caught AnalysisAgentError
        and returned 0 without re-raising, the caller (orchestrator) would
        not know the segment was skipped. The orchestrator must decide
        skip-vs-fail; that decision cannot be made silently in behavior.py.
        """
        segment = _minimal_segment(session_id="sess-dt32", project_id="proj-dt32")
        metrics = _minimal_metrics()
        fake = FakeAnalysisAgent(raise_on_segments_call=True)

        with pytest.raises(AnalysisAgentError):
            await detect_segment_flags(
                segment,
                metrics,
                session_id="sess-dt32",
                project_id="proj-dt32",
                behavior_flags_repo=repo,
                agent=fake,
            )

        # No flags must have been written.
        flags = repo.get_session_flags("sess-dt32")
        assert flags == [], f"Expected no rows after AnalysisAgentError, but found: {flags}"

    @pytest.mark.asyncio
    async def test_dt_3_3_empty_segment_analysis_returns_zero(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """DT-3.3 — Zero-flag SegmentAnalysis returns 0 without touching DB.

        Silent failure this closes: if the function always called insert_many
        even on empty flags, it would add DB overhead for every no-flag
        segment. More critically, a bug in the empty-case path could write
        a garbage row.
        """
        empty_analysis = SegmentAnalysis(
            segment_summary="No flags detected",
            flags=[],
            total_events=5,
            flagged_events=0,
        )
        segment = _minimal_segment(session_id="sess-dt33", project_id="proj-dt33")
        metrics = _minimal_metrics()
        fake = FakeAnalysisAgent(segment_outputs=[empty_analysis])

        result = await detect_segment_flags(
            segment,
            metrics,
            session_id="sess-dt33",
            project_id="proj-dt33",
            behavior_flags_repo=repo,
            agent=fake,
        )

        assert result == 0, f"Expected 0 for empty flags, got {result}"
        flags = repo.get_session_flags("sess-dt33")
        assert flags == [], f"Expected no flags in DB, got {flags}"

    @pytest.mark.asyncio
    async def test_dt_3_4_confidence_guard_at_pre_insert(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """DT-3.4 — confidence='unknown' raises ValueError before insert.

        Silent failure this closes: if the pre-insert validation loop only
        checked flag_type but skipped confidence, a bad confidence value
        would reach the repository's _guard and fail mid-batch (DC-2
        re-emerges for confidence).
        """
        # model_construct bypasses the Literal["high","medium","low"] check.
        bad_confidence_draft = BehaviorFlagDraft.model_construct(
            flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
            event_ids=["evt-bad-conf"],
            reason="detected redundant exploration",
            confidence="unknown",  # NOT in {high, medium, low}
        )
        bad_analysis = SegmentAnalysis.model_construct(
            segment_summary="One flag with bad confidence",
            flags=[bad_confidence_draft],
            total_events=5,
            flagged_events=1,
        )

        segment = _minimal_segment(session_id="sess-dt34", project_id="proj-dt34")
        metrics = _minimal_metrics()
        fake = FakeAnalysisAgent(segment_outputs=[bad_analysis])

        with pytest.raises(ValueError, match="confidence|unknown"):
            await detect_segment_flags(
                segment,
                metrics,
                session_id="sess-dt34",
                project_id="proj-dt34",
                behavior_flags_repo=repo,
                agent=fake,
            )

        counts = repo.count_by_type("proj-dt34")
        assert counts == {}, f"Expected empty DB after confidence ValueError, got: {counts}"

    @pytest.mark.asyncio
    async def test_dt_3_5_empty_event_ids_raises_before_insert(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """DT-3.5 — event_ids=[] raises ValueError before insert; DB unchanged.

        Silent failure this closes: when event_ids is empty, _make_flag_id
        previously used the literal "no-event" as the ID anchor. Multiple
        same-type empty-event flags in the same segment would collide to the
        same ID, and ON CONFLICT DO NOTHING would silently drop all but the
        first while detect_segment_flags still returned len(drafts) as the
        count. This test closes that silent data-loss path.

        After CRITICAL-2 fix: an empty event_ids list is rejected in
        validate_draft_pre_insert with ValueError before insert_many is called.
        """
        # model_construct bypasses the list[str] field so we can inject
        # an empty list that normal Pydantic construction would also allow
        # (BehaviorFlagDraft has no min_length on event_ids).
        empty_event_draft = BehaviorFlagDraft.model_construct(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=[],  # violates LLM contract
            reason="read unrelated file",
            confidence="high",
        )
        bad_analysis = SegmentAnalysis.model_construct(
            segment_summary="One flag with empty event_ids",
            flags=[empty_event_draft],
            total_events=5,
            flagged_events=1,
        )

        segment = _minimal_segment(session_id="sess-dt35", project_id="proj-dt35")
        metrics = _minimal_metrics()
        fake = FakeAnalysisAgent(segment_outputs=[bad_analysis])

        with pytest.raises(ValueError, match="event_ids.*empty|empty.*event_ids"):
            await detect_segment_flags(
                segment,
                metrics,
                session_id="sess-dt35",
                project_id="proj-dt35",
                behavior_flags_repo=repo,
                agent=fake,
            )

        # DB must be empty — no partial write before validation failure.
        counts = repo.count_by_type("proj-dt35")
        assert counts == {}, f"Expected empty DB after empty event_ids ValueError, got: {counts}"


# =====================================================================
# HAPPY-PATH TESTS
# =====================================================================


class TestHappyPaths:
    @pytest.mark.asyncio
    async def test_hp_3_a_single_segment_two_valid_flags(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """HP-3.A — Single segment with 2 valid flags. Returns 2; DB contains 2 rows."""
        draft_1 = _make_valid_draft(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["evt-001"],
            confidence="high",
        )
        draft_2 = _make_valid_draft(
            flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
            event_ids=["evt-002"],
            confidence="medium",
        )
        analysis = _make_segment_analysis_with_drafts([draft_1, draft_2])

        segment = _minimal_segment(
            segment_index=1,
            session_id="sess-hpa",
            project_id="proj-hpa",
        )
        metrics = _minimal_metrics()
        fake = FakeAnalysisAgent(segment_outputs=[analysis])

        result = await detect_segment_flags(
            segment,
            metrics,
            session_id="sess-hpa",
            project_id="proj-hpa",
            behavior_flags_repo=repo,
            agent=fake,
        )

        assert result == 2, f"Expected 2 flags persisted, got {result}"

        flags = repo.get_session_flags("sess-hpa")
        assert len(flags) == 2, f"Expected 2 rows in DB, got {len(flags)}"

        flag_types = {f.flag_type for f in flags}
        assert BehaviorFlagType.UNNECESSARY_READ in flag_types
        assert BehaviorFlagType.REDUNDANT_EXPLORATION in flag_types

        for f in flags:
            assert f.segment_index == 1, f"Expected segment_index=1, got {f.segment_index}"
            assert f.session_id == "sess-hpa"
            assert f.project_id == "proj-hpa"

        confidences = {f.flag_type: f.confidence for f in flags}
        assert confidences[BehaviorFlagType.UNNECESSARY_READ] == "high"
        assert confidences[BehaviorFlagType.REDUNDANT_EXPLORATION] == "medium"

    @pytest.mark.asyncio
    async def test_hp_3_b_idempotent_recall_deterministic_ids(
        self, repo: BehaviorFlagsRepository
    ) -> None:
        """HP-3.B — Idempotent re-call with deterministic IDs.

        Both calls return 2 (attempted count per insert_many semantics).
        Second call inserts 0 new rows (ON CONFLICT DO NOTHING).
        Total rows after both calls: 2, not 4.

        This test documents and verifies the ID generation policy:
        sha256(session_id + "|" + str(segment_index) + "|"
               + sorted(event_ids).join(",") + "|" + flag_type.value
               + "|" + reason) — truncated to 32 hex chars.
        Same inputs (same agent output → same reason, same event_ids)
        → same IDs → idempotent inserts.
        """
        draft_1 = _make_valid_draft(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["evt-001"],
            confidence="high",
        )
        draft_2 = _make_valid_draft(
            flag_type=BehaviorFlagType.MISSED_SHORTCUT,
            event_ids=["evt-002"],
            confidence="low",
        )
        analysis = _make_segment_analysis_with_drafts([draft_1, draft_2])

        segment = _minimal_segment(
            segment_index=0,
            session_id="sess-hpb",
            project_id="proj-hpb",
        )
        metrics = _minimal_metrics()

        # First call — inserts 2 flags
        fake_1 = FakeAnalysisAgent(segment_outputs=[analysis])
        result_1 = await detect_segment_flags(
            segment,
            metrics,
            session_id="sess-hpb",
            project_id="proj-hpb",
            behavior_flags_repo=repo,
            agent=fake_1,
        )
        assert result_1 == 2

        # Second call — same segment, same agent output, deterministic IDs
        fake_2 = FakeAnalysisAgent(segment_outputs=[analysis])
        result_2 = await detect_segment_flags(
            segment,
            metrics,
            session_id="sess-hpb",
            project_id="proj-hpb",
            behavior_flags_repo=repo,
            agent=fake_2,
        )
        # insert_many returns len(input) == 2 always (attempted, not new)
        assert result_2 == 2, (
            f"Expected 2 (attempted count), got {result_2}. "
            "insert_many returns len(input), not new-rows count."
        )

        # DB must have exactly 2 rows (not 4) — idempotent on deterministic IDs
        flags = repo.get_session_flags("sess-hpb")
        assert len(flags) == 2, (
            f"Expected 2 total rows after two calls (idempotent), got {len(flags)}. "
            "If IDs are non-deterministic (uuid4), this will be 4 — "
            "document the ID policy and reformulate the test accordingly."
        )
