"""Death + happy-path tests for AnalysisAgent Protocol, AnalysisAgentError,
and FakeAnalysisAgent test double (GUR-102 task-2).

Death tests come FIRST. Each death test names the silent failure mode it closes.

Death test inventory:
- DT-2.1: Protocol cannot be instantiated directly (it is a contract).
- DT-2.2: isinstance check works on conformant class; rejects bare object.
- DT-2.3: analyze_segments len contract (len(out)==len(in); empty=empty).
- DT-2.4: raise_on_segments_call raises AnalysisAgentError.
- DT-2.5: Bad SegmentAnalysis (invalid BehaviorFlagType) caught at construction.
- DT-2.6: analyze_segments with insufficient outputs raises AnalysisAgentError
           (not silent truncation).
- DT-2.7: summarize_session with no summary_output configured raises
           AnalysisAgentError (not RuntimeError).

Assumption (verified): pytest-asyncio 1.3.0 with no asyncio_mode=auto in
pyproject.toml. All async tests need pytestmark = pytest.mark.asyncio.

FakeAnalysisAgent was originally defined here; extracted to
tests/analysis/test_fake_agent.py (GUR-102 task-3) so task-3/4/5 can import
without pulling in task-2's test assertions. All tests below use the
shared module. The behaviour is identical.
"""

from __future__ import annotations

from typing import Any, cast


import pytest

from secondsight.analysis.agent import AnalysisAgent, AnalysisAgentError
from secondsight.analysis.prompts.aggregate import AggregateOutput, AggregatePattern
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import BehaviorFlagDraft, BehaviorFlagType, SegmentAnalysis

# FakeAnalysisAgent is now the shared test double; import from shared module.
from tests.analysis.test_fake_agent import FakeAnalysisAgent  # noqa: F401


# =====================================================================
# Fixture helpers
# =====================================================================


def _make_segment_analysis(segment_summary: str = "All good") -> SegmentAnalysis:
    """Minimal valid SegmentAnalysis for test setup."""
    return SegmentAnalysis(
        segment_summary=segment_summary,
        flags=[],
        total_events=1,
        flagged_events=0,
    )


def _make_segment_analysis_with_flag(
    flag_type: BehaviorFlagType = BehaviorFlagType.UNNECESSARY_READ,
) -> SegmentAnalysis:
    """SegmentAnalysis with one real flag draft."""
    return SegmentAnalysis(
        segment_summary="One flag found",
        flags=[
            BehaviorFlagDraft(
                flag_type=flag_type,
                event_ids=["evt-001"],
                reason="Read file unrelated to task",
                confidence="high",
            )
        ],
        total_events=5,
        flagged_events=1,
    )


def _make_aggregate_output() -> AggregateOutput:
    return AggregateOutput(
        patterns=[
            AggregatePattern(
                pattern_description="Reads README before starting any task",
                occurrence_count=3,
                representative_sessions=["sess-001", "sess-002"],
                convention="Avoid reading README unless task is README-related.",
            )
        ]
    )


def _make_summary_output() -> SummaryOutput:
    return SummaryOutput(
        headline="1 flag across 1 segment",
        key_findings=["Unnecessary README read detected in segment 0"],
        body="The agent read README.md before modifying a.py.",
    )


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    @pytest.mark.asyncio
    async def test_dt_2_1_protocol_cannot_be_instantiated(self) -> None:
        """DT-2.1 — AnalysisAgent() raises TypeError.

        The Protocol is a contract, not a base class. Instantiating it
        directly signals a caller mistake; if this were silently allowed,
        callers would get an object with unimplemented methods and only
        fail at call time, not at construction time.
        """
        with pytest.raises(TypeError):
            cast(Any, AnalysisAgent)()

    def test_dt_2_2_isinstance_conformant_class_true(self) -> None:
        """DT-2.2 — isinstance works for conformant class, rejects bare object.

        @runtime_checkable checks method NAMES only, not signatures.
        This test documents that limitation: a class with wrong
        async-ness or wrong signatures would still pass isinstance.
        mypy catches the rest; this test only covers runtime behavior.
        """
        fake = FakeAnalysisAgent()
        assert isinstance(fake, AnalysisAgent), (
            "FakeAnalysisAgent must satisfy isinstance(fake, AnalysisAgent). "
            "If this fails, @runtime_checkable is missing or method names differ."
        )
        assert not isinstance(object(), AnalysisAgent), (
            "bare object() must NOT satisfy isinstance check. "
            "If this fails, @runtime_checkable may not be applied."
        )

    @pytest.mark.asyncio
    async def test_dt_2_3_analyze_segments_len_contract(self) -> None:
        """DT-2.3 — len(out) == len(in); empty list returns empty list, not error.

        The silent failure here: if FakeAnalysisAgent returned the entire
        configured segment_outputs list regardless of prompts length, task-3/4/5
        code would silently receive more outputs than expected and lose the
        index alignment between prompts and results.
        """
        outputs = [
            _make_segment_analysis("seg-A"),
            _make_segment_analysis("seg-B"),
            _make_segment_analysis("seg-C"),
        ]
        fake = FakeAnalysisAgent(segment_outputs=outputs)

        result = await fake.analyze_segments(["p1", "p2", "p3"])
        assert len(result) == 3
        assert result[0].segment_summary == "seg-A"
        assert result[2].segment_summary == "seg-C"

        # Empty prompts → empty result (not an error, not the full outputs list)
        empty_result = await fake.analyze_segments([])
        assert empty_result == []

    @pytest.mark.asyncio
    async def test_dt_2_4_raise_on_segments_call_raises_analysis_agent_error(self) -> None:
        """DT-2.4 — raise_on_segments_call=True causes AnalysisAgentError.

        Silent failure: if the fake swallowed the error flag and returned
        canned outputs, task-3/4/5 tests would never exercise the caller's
        error-handling path. GUR-102 orchestrator decides skip vs. fail-loud;
        it needs a fake that can simulate the failure.
        """
        fake = FakeAnalysisAgent(
            segment_outputs=[_make_segment_analysis()],
            raise_on_segments_call=True,
        )
        with pytest.raises(AnalysisAgentError):
            await fake.analyze_segments(["prompt-text"])

    def test_dt_2_5_invalid_flag_type_raises_at_construction(self) -> None:
        """DT-2.5 — SegmentAnalysis with invalid BehaviorFlagType raises at Pydantic construction.

        This is a configuration mistake catcher: if a test sets up a fake
        with a misspelled or removed flag_type, Pydantic should catch it
        at SegmentAnalysis() call time (i.e., at test-setup), not silently
        succeed and then fail in production at LLM-output parsing time.

        Note: the test constructs the invalid SegmentAnalysis directly —
        FakeAnalysisAgent itself has no Pydantic validation of its inputs.
        The death case is Pydantic on SegmentAnalysis, not on FakeAnalysisAgent.
        """
        from pydantic import ValidationError

        with pytest.raises((ValidationError, ValueError)):
            SegmentAnalysis(
                segment_summary="Bad flag",
                flags=cast(
                    Any,
                    [
                        {
                            "flag_type": "not_a_real_flag_type",
                            "event_ids": ["evt-001"],
                            "reason": "some reason",
                            "confidence": "high",
                        }
                    ],
                ),
                total_events=1,
                flagged_events=1,
            )

    @pytest.mark.asyncio
    async def test_dt_2_6_analyze_segments_insufficient_outputs_raises_analysis_agent_error(
        self,
    ) -> None:
        """DT-2.6 — analyze_segments raises AnalysisAgentError when len(prompts) > len(outputs).

        Silent failure this closes: the previous implementation silently
        truncated results to len(segment_outputs) items when more prompts
        were requested. A test author who misconfigured the fake with fewer
        outputs than prompts would receive misaligned results and index
        errors only later in the test, far from the misconfiguration site.

        After CRITICAL-2 fix: the fake raises AnalysisAgentError immediately,
        naming the count mismatch so the test author sees the failure at
        the call site.
        """
        outputs = [_make_segment_analysis("only-one")]
        fake = FakeAnalysisAgent(segment_outputs=outputs)
        with pytest.raises(AnalysisAgentError, match="2 prompts requested but only 1"):
            await fake.analyze_segments(["prompt-1", "prompt-2"])

    @pytest.mark.asyncio
    async def test_dt_2_7_summarize_session_no_output_raises_analysis_agent_error(
        self,
    ) -> None:
        """DT-2.7 — summarize_session with no summary_output raises AnalysisAgentError.

        Silent failure this closes: the previous implementation raised
        RuntimeError on missing summary_output. A caller (e.g. task-5
        orchestrator tests) using `except AnalysisAgentError:` would silently
        bypass that RuntimeError, masking the fake misconfiguration.

        After CRITICAL-3 fix: raises AnalysisAgentError, consistent with
        the Protocol's declared exception type.
        """
        fake = FakeAnalysisAgent()  # no summary_output configured
        with pytest.raises(AnalysisAgentError, match="no summary_output configured"):
            await fake.summarize_session("any prompt text")


# =====================================================================
# HAPPY-PATH TESTS
# =====================================================================


class TestHappyPaths:
    @pytest.mark.asyncio
    async def test_hp_2_1_fake_analyze_segments_roundtrip(self) -> None:
        """HP-2.1 — configure with 3 segment_outputs; receive them in order."""
        outputs = [
            _make_segment_analysis("first"),
            _make_segment_analysis("second"),
            _make_segment_analysis("third"),
        ]
        fake = FakeAnalysisAgent(segment_outputs=outputs)
        result = await fake.analyze_segments(["a", "b", "c"])
        assert len(result) == 3
        assert result[0].segment_summary == "first"
        assert result[1].segment_summary == "second"
        assert result[2].segment_summary == "third"

    @pytest.mark.asyncio
    async def test_hp_2_2_aggregate_fake_returns_canned_output(self) -> None:
        """HP-2.2 — aggregate_flag_type returns canned AggregateOutput keyed by prompt.

        aggregate_outputs dict is keyed by prompt string verbatim.
        This means test callers pass the exact prompt text they will
        use in the call — simple and transparent, no substring logic.
        """
        canned = _make_aggregate_output()
        prompt_text = "some-flag-type-prompt-text"
        fake = FakeAnalysisAgent(aggregate_outputs={prompt_text: canned})
        result = await fake.aggregate_flag_type(prompt_text)
        assert result is canned

    @pytest.mark.asyncio
    async def test_hp_2_3_summary_fake_returns_canned_output(self) -> None:
        """HP-2.3 — summarize_session returns the canned SummaryOutput."""
        canned = _make_summary_output()
        fake = FakeAnalysisAgent(summary_output=canned)
        result = await fake.summarize_session("any prompt text")
        assert result is canned

    @pytest.mark.asyncio
    async def test_hp_2_4_raise_on_aggregate_call(self) -> None:
        """Additional death path: raise_on_aggregate_call=True raises AnalysisAgentError."""
        canned = _make_aggregate_output()
        fake = FakeAnalysisAgent(
            aggregate_outputs={"p": canned},
            raise_on_aggregate_call=True,
        )
        with pytest.raises(AnalysisAgentError):
            await fake.aggregate_flag_type("p")

    @pytest.mark.asyncio
    async def test_hp_2_5_raise_on_summary_call(self) -> None:
        """Additional death path: raise_on_summary_call=True raises AnalysisAgentError."""
        canned = _make_summary_output()
        fake = FakeAnalysisAgent(
            summary_output=canned,
            raise_on_summary_call=True,
        )
        with pytest.raises(AnalysisAgentError):
            await fake.summarize_session("any prompt")

    @pytest.mark.asyncio
    async def test_hp_2_6_analyze_segments_single_item(self) -> None:
        """Single-segment callers use len==1 form as documented in the Protocol."""
        output = _make_segment_analysis_with_flag()
        fake = FakeAnalysisAgent(segment_outputs=[output])
        result = await fake.analyze_segments(["single prompt"])
        assert len(result) == 1
        assert len(result[0].flags) == 1
        assert result[0].flags[0].flag_type == BehaviorFlagType.UNNECESSARY_READ

    def test_hp_2_7_fake_is_instance_of_protocol(self) -> None:
        """Explicit isinstance smoke test — confirms @runtime_checkable works."""
        fake = FakeAnalysisAgent()
        assert isinstance(fake, AnalysisAgent)

    @pytest.mark.asyncio
    async def test_hp_2_8_aggregate_outputs_missing_key_raises_key_error(self) -> None:
        """Calling aggregate_flag_type with a prompt not in aggregate_outputs raises KeyError.

        This is intentional: a misconfigured fake should fail loudly at test
        time, not silently return None or a default. The caller (test) is
        responsible for setting up all needed keys.
        """
        fake = FakeAnalysisAgent(aggregate_outputs={"key-a": _make_aggregate_output()})
        with pytest.raises(KeyError):
            await fake.aggregate_flag_type("key-b")

    @pytest.mark.asyncio
    async def test_hp_2_9_analyze_segments_more_outputs_than_prompts_ok(self) -> None:
        """Configuring more outputs than prompts is valid — extras are unused.

        This is the inverse of DT-2.6: having spare configured outputs is
        not an error. The fake returns only as many outputs as prompts requested.
        """
        outputs = [
            _make_segment_analysis("first"),
            _make_segment_analysis("second"),
            _make_segment_analysis("spare"),
        ]
        fake = FakeAnalysisAgent(segment_outputs=outputs)
        result = await fake.analyze_segments(["only-one-prompt"])
        assert len(result) == 1
        assert result[0].segment_summary == "first"
