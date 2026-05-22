"""Tests for deterministic directive lifecycle weight policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from secondsight.analysis.schemas import Directive, DirectiveStatus, DirectiveType
from secondsight.config.schema import DirectiveLifecycleConfig
from secondsight.feedback.directive_policy import (
    DirectiveLifecycleSignal,
    evaluate_directive_policy_with_config,
    evaluate_directive_policy,
)


def _now() -> datetime:
    return datetime(2026, 5, 22, 18, 0, 0, tzinfo=timezone.utc)


def _directive(
    *,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
    weight: float = 0.7,
    miss_streak: int = 0,
    revision_count: int = 0,
) -> Directive:
    return Directive(
        id="dir-1",
        project_id="proj-1",
        type=DirectiveType.CONVENTION,
        status=status,
        instruction="Read the exact target file first",
        source_flag_type="unnecessary_read",
        identity_key="lineage-1",
        weight=weight,
        miss_streak=miss_streak,
        revision_count=revision_count,
        created_at=_now(),
        updated_at=_now(),
    )


class TestDirectivePolicy:
    def test_source_flag_seen_without_identity_repromotion_holds_weight_and_marks_revision(
        self,
    ) -> None:
        decision = evaluate_directive_policy(
            DirectiveLifecycleSignal(
                directive=_directive(weight=0.55, miss_streak=1),
                now=_now(),
                same_identity_repromoted=False,
                source_flag_seen=True,
            )
        )

        assert decision.new_weight == 0.55
        assert decision.new_miss_streak == 0
        assert decision.new_status is DirectiveStatus.ACTIVE
        assert decision.should_revise is True

    def test_disabled_directives_are_excluded_from_policy_evaluation(self) -> None:
        decision = evaluate_directive_policy(
            DirectiveLifecycleSignal(
                directive=_directive(status=DirectiveStatus.DISABLED, weight=0.4),
                now=_now(),
                same_identity_repromoted=False,
                source_flag_seen=False,
            )
        )

        assert decision.new_weight == 0.4
        assert decision.new_status is DirectiveStatus.DISABLED
        assert decision.should_revise is False
        assert decision.applied is False

    def test_consecutive_misses_decay_to_obsolete(self) -> None:
        decision = evaluate_directive_policy(
            DirectiveLifecycleSignal(
                directive=_directive(weight=0.25, miss_streak=2),
                now=_now(),
                same_identity_repromoted=False,
                source_flag_seen=False,
            )
        )

        assert decision.new_weight < 0.25
        assert decision.new_status is DirectiveStatus.OBSOLETE

    def test_revision_cap_exhaustion_transitions_to_stalled(self) -> None:
        decision = evaluate_directive_policy(
            DirectiveLifecycleSignal(
                directive=_directive(weight=0.55, revision_count=3),
                now=_now(),
                same_identity_repromoted=False,
                source_flag_seen=True,
            )
        )

        assert decision.new_status is DirectiveStatus.STALLED
        assert decision.should_revise is False

    def test_custom_config_thresholds_control_decay(self) -> None:
        decision = evaluate_directive_policy_with_config(
            DirectiveLifecycleSignal(
                directive=_directive(weight=0.55, miss_streak=1),
                now=_now(),
                same_identity_repromoted=False,
                source_flag_seen=False,
            ),
            DirectiveLifecycleConfig(
                capacity_ceiling=15,
                boost_delta=0.2,
                decay_delta=0.2,
                miss_grace=2,
                obsolete_threshold=0.4,
                revision_cap=3,
            ),
        )

        assert decision.new_weight == pytest.approx(0.35)
        assert decision.new_status is DirectiveStatus.OBSOLETE
