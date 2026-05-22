"""Deterministic policy layer for directive lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from secondsight.analysis.schemas import Directive, DirectiveStatus
from secondsight.config.schema import DirectiveLifecycleConfig


@dataclass(frozen=True, slots=True)
class DirectiveLifecycleSignal:
    directive: Directive
    now: datetime
    same_identity_repromoted: bool
    source_flag_seen: bool


@dataclass(frozen=True, slots=True)
class DirectivePolicyDecision:
    new_weight: float
    new_status: DirectiveStatus
    new_miss_streak: int
    last_promoted_at: datetime | None
    last_source_flag_seen_at: datetime | None
    should_revise: bool
    applied: bool
    reason: str


def evaluate_directive_policy(signal: DirectiveLifecycleSignal) -> DirectivePolicyDecision:
    return evaluate_directive_policy_with_config(signal, DirectiveLifecycleConfig())


def evaluate_directive_policy_with_config(
    signal: DirectiveLifecycleSignal,
    config: DirectiveLifecycleConfig,
) -> DirectivePolicyDecision:
    directive = signal.directive

    if directive.status is DirectiveStatus.DISABLED:
        return DirectivePolicyDecision(
            new_weight=directive.weight,
            new_status=directive.status,
            new_miss_streak=directive.miss_streak,
            last_promoted_at=directive.last_promoted_at,
            last_source_flag_seen_at=directive.last_source_flag_seen_at,
            should_revise=False,
            applied=False,
            reason="disabled_excluded",
        )

    if signal.same_identity_repromoted:
        return DirectivePolicyDecision(
            new_weight=min(1.0, directive.weight + config.boost_delta),
            new_status=DirectiveStatus.ACTIVE,
            new_miss_streak=0,
            last_promoted_at=signal.now,
            last_source_flag_seen_at=signal.now,
            should_revise=False,
            applied=True,
            reason="identity_repromoted",
        )

    if signal.source_flag_seen:
        stalled = directive.revision_count >= config.revision_cap
        return DirectivePolicyDecision(
            new_weight=directive.weight,
            new_status=DirectiveStatus.STALLED if stalled else directive.status,
            new_miss_streak=0,
            last_promoted_at=directive.last_promoted_at,
            last_source_flag_seen_at=signal.now,
            should_revise=not stalled,
            applied=True,
            reason="source_flag_seen_without_identity",
        )

    new_miss_streak = directive.miss_streak + 1
    new_weight = directive.weight
    if new_miss_streak >= config.miss_grace:
        new_weight = max(0.0, directive.weight - config.decay_delta)
    new_status = directive.status
    if new_weight < config.obsolete_threshold:
        new_status = DirectiveStatus.OBSOLETE

    return DirectivePolicyDecision(
        new_weight=new_weight,
        new_status=new_status,
        new_miss_streak=new_miss_streak,
        last_promoted_at=directive.last_promoted_at,
        last_source_flag_seen_at=directive.last_source_flag_seen_at,
        should_revise=False,
        applied=True,
        reason="decay_due_to_absent_source_flag",
    )
