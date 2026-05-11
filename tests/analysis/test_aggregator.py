"""Death + happy-path tests for aggregate_project_flags and compute_identity_key
(GUR-102 task-4).

Death tests come FIRST. Each death test names the silent failure mode it closes.

Death test inventory:
- DT-4.1: DC-3 — deterministic tie-break at top_n boundary. Re-runs pick the
           same pattern at rank 15 when three patterns share occurrence_count=5.
- DT-4.2: DC-6 — two patterns, same flag_type, distinct identity_key from
           distinct representative_sessions.
- DT-4.3: DG-2.1 — partial step-2 failure writes nothing. First call succeeds,
           second raises AnalysisAgentError; no directives written.
- DT-4.4: DC-5 — flags_read disclosed in result (not silently omitted).
           Simulates retention purge by configuring the fake repo to return
           fewer flags than originally inserted.
- DT-4.5: Empty project (zero flags) → result has all zeros; no LLM calls made.
- DT-4.6: compute_identity_key stability — same inputs → same hash; session
           order does not matter; flag_type .value used (not enum object repr).
- DT-4.7: Negative top_n raises ValueError immediately (loud misconfiguration
           failure rather than silent empty-result no-op).

Happy-path tests:
- HP-4.A: Aggregate, then re-run idempotent via identity_key. First call creates
           K directives; second call with same inputs UPSERTs (no row count change).
- HP-4.B: top_n bound respected. 25 patterns → exactly 15 directives upserted.

Assumption (verified): pytest-asyncio with @pytest.mark.asyncio on each async test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.analysis.prompts.aggregate import AggregateOutput, AggregatePattern, FlagSummary, build_aggregate_prompt
from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_repository import DirectivesRepository
from tests.analysis._fake_agent import FakeAnalysisAgent

# --- import module under test ---
# These will FAIL until aggregator.py is implemented. Expected red phase.
from secondsight.analysis.aggregator import (
    DEFAULT_CONVENTION_TOP_N,
    AggregateProjectResult,
    aggregate_project_flags,
    compute_identity_key,
)


# =====================================================================
# Shared constants
# =====================================================================

_PROJECT_ID = "proj-aggregator-test"
_NOW = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[DBEngine]:
    eng = DBEngine(tmp_path / "intel.db")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def flags_repo(db_engine: DBEngine) -> BehaviorFlagsRepository:
    r = BehaviorFlagsRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def directives_repo(db_engine: DBEngine) -> DirectivesRepository:
    r = DirectivesRepository(db_engine)
    r.create_schema()
    return r


# =====================================================================
# Helper factories
# =====================================================================


def _make_flag(
    project_id: str = _PROJECT_ID,
    session_id: str = "sess-001",
    flag_type: BehaviorFlagType = BehaviorFlagType.UNNECESSARY_READ,
    flag_id_suffix: str = "a",
) -> BehaviorFlag:
    return BehaviorFlag(
        id=f"flag-{flag_id_suffix}",
        project_id=project_id,
        session_id=session_id,
        segment_index=0,
        flag_type=flag_type,
        event_ids=["evt-1"],
        intent_summary="Read unrelated file",
        reason=f"Reason for {flag_id_suffix}",
        confidence="high",
        created_at=_NOW,
    )


def _make_pattern(
    pattern_description: str,
    occurrence_count: int,
    representative_sessions: list[str] | None = None,
    convention: str = "Do not do this.",
) -> AggregatePattern:
    return AggregatePattern(
        pattern_description=pattern_description,
        occurrence_count=occurrence_count,
        representative_sessions=representative_sessions or ["sess-001"],
        convention=convention,
    )


def _count_directives(engine: DBEngine) -> int:
    """Count all rows in directives table."""
    from secondsight.storage.directives_table import directives
    stmt = sa.select(sa.func.count()).select_from(directives)
    with engine.engine.connect() as conn:
        return conn.execute(stmt).scalar() or 0


def _get_directive_instructions(engine: DBEngine) -> list[str]:
    """Return instructions of all directives ordered by identity_key."""
    from secondsight.storage.directives_table import directives
    stmt = sa.select(directives.c.instruction).order_by(directives.c.identity_key)
    with engine.engine.connect() as conn:
        return [row[0] for row in conn.execute(stmt)]


def _get_directive_identity_keys(engine: DBEngine) -> list[str]:
    """Return identity_keys of all directives."""
    from secondsight.storage.directives_table import directives
    stmt = sa.select(directives.c.identity_key).order_by(directives.c.identity_key)
    with engine.engine.connect() as conn:
        return [row[0] for row in conn.execute(stmt)]


# =====================================================================
# Custom partial-failure agent for DT-4.3
# =====================================================================


class _PartialFailureAgent:
    """FakeAnalysisAgent variant: succeeds on first call, raises on second.

    aggregate_outputs_list: ordered list of AggregateOutput for successive
    calls. The second call always raises AnalysisAgentError regardless of
    the outputs list contents.
    """

    def __init__(self, first_output: AggregateOutput) -> None:
        self._first_output = first_output
        self._call_count = 0

    async def analyze_segments(self, prompts: Sequence[str]) -> list:
        raise NotImplementedError("not used in this test")

    async def aggregate_flag_type(self, prompt: str) -> AggregateOutput:
        self._call_count += 1
        if self._call_count == 1:
            return self._first_output
        raise AnalysisAgentError(
            "_PartialFailureAgent: configured to fail on second call"
        )

    async def summarize_session(self, prompt: str):
        raise NotImplementedError("not used in this test")


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDT41DeterministicTieBreak:
    """DT-4.1 (= DT-1.3) — DC-3: deterministic tie-break at top_n boundary.

    20 patterns: ranks 1-13 have distinct high counts; ranks 14, 15, 16
    share occurrence_count=5. Two separate runs must select the identical
    pattern at rank 15 — verified by comparing upserted instructions.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_same_pattern_selected_at_rank_15_across_reruns(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        # We need flags to read; insert one flag for UNNECESSARY_READ.
        flag = _make_flag(flag_id_suffix="tiebreak-1")
        flags_repo.insert(flag)

        # Build 20 patterns for UNNECESSARY_READ.
        # Ranks 1-13: counts 20 down to 8 (distinct)
        # Ranks 14, 15, 16: count=5 — tie zone
        # Ranks 17-20: count=2
        patterns: list[AggregatePattern] = []
        for i, count in enumerate(range(20, 7, -1)):  # 20,19,...,8 → 13 items
            patterns.append(
                _make_pattern(
                    pattern_description=f"Pattern high-{i:02d}",
                    occurrence_count=count,
                    representative_sessions=[f"sess-high-{i}"],
                    convention=f"Convention high-{i}",
                )
            )
        # Three tie patterns — descriptions chosen so alphabetical order gives
        # tie-a < tie-b < tie-c. The aggregator must pick tie-a and tie-b
        # (positions 14 and 15) and drop tie-c (position 16).
        for label in ("tie-aaa", "tie-bbb", "tie-ccc"):
            patterns.append(
                _make_pattern(
                    pattern_description=f"Pattern {label}",
                    occurrence_count=5,
                    representative_sessions=[f"sess-{label}"],
                    convention=f"Convention {label}",
                )
            )
        # 4 low-count patterns
        for i in range(4):
            patterns.append(
                _make_pattern(
                    pattern_description=f"Pattern low-{i:02d}",
                    occurrence_count=2,
                    representative_sessions=[f"sess-low-{i}"],
                    convention=f"Convention low-{i}",
                )
            )

        assert len(patterns) == 20

        aggregate_output = AggregateOutput(patterns=patterns)
        flag_summaries = [
            FlagSummary(
                session_id=flag.session_id,
                segment_summary=flag.intent_summary,
                reason=flag.reason,
            )
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, flag_summaries
        )

        # Run 1
        agent1 = FakeAnalysisAgent(
            aggregate_outputs={prompt_key: aggregate_output}
        )
        result1 = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent1,
        )
        assert result1.directives_upserted == DEFAULT_CONVENTION_TOP_N

        # Capture the identity keys from run 1
        keys_after_run1 = _get_directive_identity_keys(db_engine)

        # Run 2 — same inputs, same agent
        agent2 = FakeAnalysisAgent(
            aggregate_outputs={prompt_key: aggregate_output}
        )
        result2 = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent2,
        )
        assert result2.directives_upserted == DEFAULT_CONVENTION_TOP_N

        # The chosen identity keys must be identical — same tie-break selection.
        keys_after_run2 = _get_directive_identity_keys(db_engine)
        assert keys_after_run1 == keys_after_run2

        # Specifically verify that "Pattern tie-ccc" (alphabetically last in
        # the tie group) was NOT upserted — only tie-aaa and tie-bbb made top-15.
        from secondsight.storage.directives_table import directives as directives_table
        stmt = sa.select(directives_table.c.instruction).where(
            directives_table.c.instruction.contains("Convention tie-ccc")
        )
        with db_engine.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        assert rows == [], (
            "Pattern tie-ccc should NOT be in directives (rank 16 > top_n=15)"
        )


class TestDT42DistinctIdentityKeys:
    """DT-4.2 (= DT-1.5) — DC-6: two patterns, same flag_type, distinct identity_key.

    Two AggregatePattern for UNNECESSARY_READ with overlapping but distinct
    representative_sessions. Both must produce distinct identity_keys and
    be separately persisted. Re-run must not duplicate rows.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_two_patterns_same_flag_type_distinct_keys(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        # Insert two flags
        flag1 = _make_flag(session_id="sess-A", flag_id_suffix="dt42-1")
        flag2 = _make_flag(session_id="sess-B", flag_id_suffix="dt42-2")
        flags_repo.insert(flag1)
        flags_repo.insert(flag2)

        pattern_A = _make_pattern(
            pattern_description="Pattern A",
            occurrence_count=3,
            representative_sessions=["sess-A", "sess-C"],  # overlapping
            convention="Convention A",
        )
        pattern_B = _make_pattern(
            pattern_description="Pattern B",
            occurrence_count=2,
            representative_sessions=["sess-B", "sess-C"],  # overlapping with A on sess-C
            convention="Convention B",
        )

        aggregate_output = AggregateOutput(patterns=[pattern_A, pattern_B])
        flag_summaries = [
            FlagSummary(
                session_id=f.session_id,
                segment_summary=f.intent_summary,
                reason=f.reason,
            )
            for f in [flag1, flag2]
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, flag_summaries
        )

        agent = FakeAnalysisAgent(aggregate_outputs={prompt_key: aggregate_output})
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
        )
        assert result.directives_upserted == 2

        # Both rows exist with DISTINCT identity_keys
        keys = _get_directive_identity_keys(db_engine)
        assert len(keys) == 2, f"Expected 2 directives, got {len(keys)}"
        assert len(set(keys)) == 2, f"Expected 2 DISTINCT identity_keys, got {keys}"

        # Verify keys match compute_identity_key
        expected_key_A = compute_identity_key(
            _PROJECT_ID,
            BehaviorFlagType.UNNECESSARY_READ,
            pattern_A.representative_sessions,
        )
        expected_key_B = compute_identity_key(
            _PROJECT_ID,
            BehaviorFlagType.UNNECESSARY_READ,
            pattern_B.representative_sessions,
        )
        assert set(keys) == {expected_key_A, expected_key_B}

        # Re-run — still 2 rows (UPSERT, no duplicate)
        agent2 = FakeAnalysisAgent(aggregate_outputs={prompt_key: aggregate_output})
        result2 = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent2,
        )
        assert result2.directives_upserted == 2
        assert _count_directives(db_engine) == 2, "Re-run must not create duplicate rows"


class TestDT43PartialFailureWritesNothing:
    """DT-4.3 (= DG-2.1) — Partial step-2 failure writes nothing.

    First aggregate_flag_type call succeeds; second raises AnalysisAgentError.
    No directives must be written. The aggregator is all-or-nothing.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_partial_step2_failure_leaves_no_directives(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        # Insert flags for TWO different flag types so two LLM calls are made.
        flag_read = _make_flag(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            flag_id_suffix="dt43-read",
            session_id="sess-X",
        )
        flag_redundant = _make_flag(
            flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
            flag_id_suffix="dt43-redundant",
            session_id="sess-Y",
        )
        flags_repo.insert(flag_read)
        flags_repo.insert(flag_redundant)

        agent = _PartialFailureAgent(
            first_output=AggregateOutput(
                patterns=[
                    _make_pattern(
                        "Pattern first flag type",
                        occurrence_count=2,
                        representative_sessions=["sess-X"],
                    )
                ]
            )
        )

        directives_count_before = _count_directives(db_engine)

        with pytest.raises(AnalysisAgentError):
            await aggregate_project_flags(
                _PROJECT_ID,
                behavior_flags_repo=flags_repo,
                directives_repo=directives_repo,
                agent=agent,
            )

        directives_count_after = _count_directives(db_engine)
        assert directives_count_after == directives_count_before, (
            "All-or-nothing violated: directives were written before the failure"
        )


class TestDT44FlagsReadDisclosure:
    """DT-4.4 (= DT-1.8) — DC-5: flags_read disclosed in result.

    Simulates a retention purge by making the repo return fewer flags
    than originally inserted. The result.flags_read must equal what the
    repo actually returned, not what was "inserted historically".
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_flags_read_equals_repo_returned_count(
        self,
        db_engine: DBEngine,
        directives_repo: DirectivesRepository,
    ) -> None:
        # Instead of using the real repo, we use a fake repo that simulates
        # 70 flags surviving retention purge (100 inserted, 30 purged).
        # We build flag summaries manually.

        surviving_flags = [
            BehaviorFlag(
                id=f"flag-retained-{i}",
                project_id=_PROJECT_ID,
                session_id=f"sess-{i:03d}",
                segment_index=0,
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                event_ids=["evt-1"],
                intent_summary="Retained flag",
                reason=f"Reason {i}",
                confidence="medium",
                created_at=_NOW,
            )
            for i in range(70)
        ]

        class _FakeFlagsRepo:
            """Simulates post-purge repository returning only 70 flags."""

            def get_project_flags_by_type(
                self, project_id: str, flag_type: BehaviorFlagType
            ) -> list[BehaviorFlag]:
                if flag_type == BehaviorFlagType.UNNECESSARY_READ:
                    return list(surviving_flags)
                return []

        fake_repo = _FakeFlagsRepo()

        # Build the expected prompt key for the surviving flags
        flag_summaries = [
            FlagSummary(
                session_id=f.session_id,
                segment_summary=f.intent_summary,
                reason=f.reason,
            )
            for f in surviving_flags
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, flag_summaries
        )

        agent = FakeAnalysisAgent(
            aggregate_outputs={
                prompt_key: AggregateOutput(
                    patterns=[
                        _make_pattern(
                            "Retained pattern",
                            occurrence_count=70,
                            representative_sessions=[f"sess-{i:03d}" for i in range(5)],
                        )
                    ]
                )
            }
        )

        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=fake_repo,  # type: ignore[arg-type]
            directives_repo=directives_repo,
            agent=agent,
        )

        assert result.flags_read == 70, (
            f"DC-5: flags_read must equal repo-returned count (70), got {result.flags_read}"
        )


class TestDT45EmptyProject:
    """DT-4.5 — Empty project (zero flags). All zeros; no LLM calls."""

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_empty_project_zero_result_no_llm_calls(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        # Track whether agent was called
        call_log: list[str] = []

        class _TrackingAgent:
            async def analyze_segments(self, prompts):
                raise NotImplementedError
            async def aggregate_flag_type(self, prompt: str) -> AggregateOutput:
                call_log.append(prompt)
                return AggregateOutput(patterns=[])
            async def summarize_session(self, prompt):
                raise NotImplementedError

        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=_TrackingAgent(),
        )

        assert result.calls_made == 0
        assert result.flags_read == 0
        assert result.patterns_emerged == 0
        assert result.directives_upserted == 0
        assert call_log == [], "No LLM calls should be made for an empty project"


class TestDT46IdentityKeyStability:
    """DT-4.6 — compute_identity_key is stable and order-independent."""

    def test_same_inputs_same_hash(self) -> None:
        key1 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-A", "sess-B"]
        )
        key2 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-A", "sess-B"]
        )
        assert key1 == key2

    def test_session_order_does_not_matter(self) -> None:
        key1 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-A", "sess-B"]
        )
        key2 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-B", "sess-A"]
        )
        assert key1 == key2, "Session order must not affect identity_key"

    def test_flag_type_value_not_enum_object(self) -> None:
        """Hash uses flag_type.value (string), not repr of enum object.

        This ensures resilience to enum re-ordering or repr changes.
        The hash must equal sha256(project_id + "|" + value + "|" + sorted_sessions_joined).
        """
        flag_type = BehaviorFlagType.UNNECESSARY_READ
        sessions = ["sess-A", "sess-B"]
        computed = compute_identity_key(_PROJECT_ID, flag_type, sessions)

        # Manually compute expected hash (security-privacy-review MEDIUM-3:
        # project_id is now part of the hash input).
        sorted_sessions = sorted(sessions)
        raw = f"{_PROJECT_ID}|{flag_type.value}|{','.join(sorted_sessions)}"
        expected = hashlib.sha256(raw.encode()).hexdigest()

        assert computed == expected, (
            f"identity_key must be sha256 of project_id + '|' + flag_type.value + "
            f"'|' + sorted sessions. Got {computed!r}, expected {expected!r}"
        )

    def test_different_sessions_different_hash(self) -> None:
        key1 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-A"]
        )
        key2 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-B"]
        )
        assert key1 != key2

    def test_different_flag_types_different_hash(self) -> None:
        key1 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-A"]
        )
        key2 = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.REDUNDANT_EXPLORATION, ["sess-A"]
        )
        assert key1 != key2

    def test_different_projects_different_hash(self) -> None:
        """Security-privacy-review MEDIUM-3: distinct projects must produce
        distinct hashes even when flag_type + sessions are identical."""
        key1 = compute_identity_key(
            "proj-alpha", BehaviorFlagType.UNNECESSARY_READ, ["sess-A"]
        )
        key2 = compute_identity_key(
            "proj-beta", BehaviorFlagType.UNNECESSARY_READ, ["sess-A"]
        )
        assert key1 != key2, (
            "Cross-project isolation must be structural in the hash, "
            "not solely enforced by the DB UNIQUE constraint."
        )

    def test_empty_sessions_stable(self) -> None:
        """Empty session list produces a stable hash (not error)."""
        key1 = compute_identity_key(_PROJECT_ID, BehaviorFlagType.MISSED_SHORTCUT, [])
        key2 = compute_identity_key(_PROJECT_ID, BehaviorFlagType.MISSED_SHORTCUT, [])
        assert key1 == key2

        # Verify it matches manual computation (security-privacy-review
        # MEDIUM-3: project_id is now the first segment of the hash input).
        raw = f"{_PROJECT_ID}|{BehaviorFlagType.MISSED_SHORTCUT.value}|"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert key1 == expected


class TestDT47NegativeTopNRaisesValueError:
    """DT-4.7 — Negative top_n raises ValueError immediately.

    Silent failure mode closed: top_n=-1 would silently produce 0 directives
    via Python list slice `[:−1]` (truncates last element) or an all-empty
    result, giving the caller no indication of misconfiguration. ValueError
    on negative values makes this loud.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_negative_top_n_raises_value_error(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        with pytest.raises(ValueError, match="top_n"):
            await aggregate_project_flags(
                _PROJECT_ID,
                behavior_flags_repo=flags_repo,
                directives_repo=directives_repo,
                agent=FakeAnalysisAgent(aggregate_outputs={}),
                top_n=-1,
            )

    @pytest.mark.asyncio
    async def test_top_n_zero_produces_zero_directives_no_error(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        """top_n=0 is a valid (silent no-op) caller choice. No error raised."""
        flag = _make_flag(flag_id_suffix="dt47-zero")
        flags_repo.insert(flag)

        summaries = [
            FlagSummary(
                session_id=flag.session_id,
                segment_summary=flag.intent_summary,
                reason=flag.reason,
            )
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries
        )
        agent = FakeAnalysisAgent(
            aggregate_outputs={
                prompt_key: AggregateOutput(
                    patterns=[_make_pattern("P", 1, ["sess-001"])]
                )
            }
        )
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
            top_n=0,
        )
        assert result.directives_upserted == 0
        assert _count_directives(db_engine) == 0


# =====================================================================
# HAPPY-PATH TESTS
# =====================================================================


class TestHPA4IdempotentRerun:
    """HP-4.A — Aggregate, then re-run idempotent via identity_key.

    First run creates K directives; second run with same inputs UPSERTs
    (no new rows). The identity_key is preserved; convention text may
    differ (LLM nondeterminism), but row count does not change.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_rerun_does_not_create_duplicate_rows(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        flag = _make_flag(flag_id_suffix="hp4a-1")
        flags_repo.insert(flag)

        summaries = [
            FlagSummary(
                session_id=flag.session_id,
                segment_summary=flag.intent_summary,
                reason=flag.reason,
            )
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries
        )

        first_output = AggregateOutput(
            patterns=[
                _make_pattern("Pattern X", 5, ["sess-001"], "Convention version 1")
            ]
        )
        second_output = AggregateOutput(
            patterns=[
                # Same pattern_description + sessions (same identity_key),
                # but convention text changed (LLM nondeterminism)
                _make_pattern("Pattern X", 5, ["sess-001"], "Convention version 2")
            ]
        )

        agent1 = FakeAnalysisAgent(aggregate_outputs={prompt_key: first_output})
        result1 = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent1,
        )
        assert result1.directives_upserted == 1
        count_after_run1 = _count_directives(db_engine)

        agent2 = FakeAnalysisAgent(aggregate_outputs={prompt_key: second_output})
        result2 = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent2,
        )
        assert result2.directives_upserted == 1
        count_after_run2 = _count_directives(db_engine)

        assert count_after_run1 == count_after_run2, (
            "UPSERT on re-run must not create new rows"
        )

        # identity_key must be the same between runs
        keys = _get_directive_identity_keys(db_engine)
        assert len(keys) == 1
        expected_key = compute_identity_key(
            _PROJECT_ID, BehaviorFlagType.UNNECESSARY_READ, ["sess-001"]
        )
        assert keys[0] == expected_key


class TestHPB4TopNBound:
    """HP-4.B — top_n bound respected. 25 patterns → 15 directives upserted."""

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_25_patterns_yields_15_directives(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        flag = _make_flag(flag_id_suffix="hp4b-1")
        flags_repo.insert(flag)

        # 25 patterns with distinct occurrence_counts 25..1
        patterns = [
            _make_pattern(
                pattern_description=f"Pattern {i:03d}",
                occurrence_count=25 - i,
                representative_sessions=[f"sess-hp4b-{i}"],
                convention=f"Convention {i}",
            )
            for i in range(25)
        ]
        assert len(patterns) == 25

        summaries = [
            FlagSummary(
                session_id=flag.session_id,
                segment_summary=flag.intent_summary,
                reason=flag.reason,
            )
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries
        )

        agent = FakeAnalysisAgent(
            aggregate_outputs={prompt_key: AggregateOutput(patterns=patterns)}
        )
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
        )

        assert result.patterns_emerged == 25
        assert result.directives_upserted == 15  # DEFAULT_CONVENTION_TOP_N
        assert _count_directives(db_engine) == 15

    @pytest.mark.asyncio
    async def test_custom_top_n_respected(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        """top_n parameter overrides default."""
        flag = _make_flag(flag_id_suffix="hp4b-custom-1")
        flags_repo.insert(flag)

        patterns = [
            _make_pattern(
                pattern_description=f"Pattern {i:02d}",
                occurrence_count=10 - i,
                representative_sessions=[f"sess-custom-{i}"],
            )
            for i in range(10)
        ]

        summaries = [
            FlagSummary(
                session_id=flag.session_id,
                segment_summary=flag.intent_summary,
                reason=flag.reason,
            )
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries
        )

        agent = FakeAnalysisAgent(
            aggregate_outputs={prompt_key: AggregateOutput(patterns=patterns)}
        )
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
            top_n=5,
        )

        assert result.directives_upserted == 5
        assert _count_directives(db_engine) == 5


class TestResultFields:
    """Verify AggregateProjectResult fields are populated correctly."""

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_result_fields_single_flag_type(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        flag = _make_flag(flag_id_suffix="result-1")
        flags_repo.insert(flag)

        summaries = [
            FlagSummary(
                session_id=flag.session_id,
                segment_summary=flag.intent_summary,
                reason=flag.reason,
            )
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries
        )

        agent = FakeAnalysisAgent(
            aggregate_outputs={
                prompt_key: AggregateOutput(
                    patterns=[
                        _make_pattern("P1", 3, ["sess-001"]),
                        _make_pattern("P2", 2, ["sess-002"]),
                    ]
                )
            }
        )
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
        )

        assert result.project_id == _PROJECT_ID
        assert result.calls_made == 1
        assert result.flags_read == 1
        assert result.patterns_emerged == 2
        assert result.directives_upserted == 2
        assert isinstance(result.aggregated_at, datetime)

    @pytest.mark.asyncio
    async def test_frequency_field_set_on_directive(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        """frequency = occurrence_count / flags_read for this project."""
        # 4 flags total; pattern has occurrence_count=2 → frequency=0.5
        # Build flags first, then derive summaries from them (matching aggregator logic).
        inserted_flags = []
        for i in range(4):
            flag = _make_flag(
                session_id=f"sess-freq-{i}",
                flag_id_suffix=f"freq-{i}",
            )
            flags_repo.insert(flag)
            inserted_flags.append(flag)

        # Derive summaries the same way the aggregator does:
        # BehaviorFlag.intent_summary → FlagSummary.segment_summary
        summaries = [
            FlagSummary(
                session_id=f.session_id,
                segment_summary=f.intent_summary,
                reason=f.reason,
            )
            for f in inserted_flags
        ]
        prompt_key = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries
        )

        agent = FakeAnalysisAgent(
            aggregate_outputs={
                prompt_key: AggregateOutput(
                    patterns=[_make_pattern("Pattern freq", 2, ["sess-freq-0"])]
                )
            }
        )
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
        )

        assert result.flags_read == 4
        # Check directive frequency in DB
        from secondsight.storage.directives_table import directives as directives_table
        stmt = sa.select(directives_table.c.frequency)
        with db_engine.engine.connect() as conn:
            freq = conn.execute(stmt).scalar()
        assert freq == pytest.approx(2.0 / 4.0)

    @pytest.mark.asyncio
    async def test_multi_flag_type_calls_made_count(
        self,
        db_engine: DBEngine,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
    ) -> None:
        """calls_made counts per-flag-type LLM invocations, not total patterns."""
        # Two flag types, one flag each
        flag_read = _make_flag(
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            flag_id_suffix="multi-read",
            session_id="sess-multi-1",
        )
        flag_redundant = _make_flag(
            flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
            flag_id_suffix="multi-redundant",
            session_id="sess-multi-2",
        )
        flags_repo.insert(flag_read)
        flags_repo.insert(flag_redundant)

        summaries_read = [
            FlagSummary(
                session_id=flag_read.session_id,
                segment_summary=flag_read.intent_summary,
                reason=flag_read.reason,
            )
        ]
        summaries_redundant = [
            FlagSummary(
                session_id=flag_redundant.session_id,
                segment_summary=flag_redundant.intent_summary,
                reason=flag_redundant.reason,
            )
        ]
        prompt_key_read = build_aggregate_prompt(
            BehaviorFlagType.UNNECESSARY_READ, summaries_read
        )
        prompt_key_redundant = build_aggregate_prompt(
            BehaviorFlagType.REDUNDANT_EXPLORATION, summaries_redundant
        )

        agent = FakeAnalysisAgent(
            aggregate_outputs={
                prompt_key_read: AggregateOutput(
                    patterns=[_make_pattern("Read pattern", 1, ["sess-multi-1"])]
                ),
                prompt_key_redundant: AggregateOutput(
                    patterns=[_make_pattern("Redundant pattern", 1, ["sess-multi-2"])]
                ),
            }
        )
        result = await aggregate_project_flags(
            _PROJECT_ID,
            behavior_flags_repo=flags_repo,
            directives_repo=directives_repo,
            agent=agent,
        )

        assert result.calls_made == 2
        assert result.flags_read == 2
        assert result.patterns_emerged == 2
        assert result.directives_upserted == 2
