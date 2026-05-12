"""Death tests for directive lifecycle state machine (GUR-105, P3A-4).

Death cases:
- DT-1: Superseded is terminal — no transition out is allowed.
- DT-2: Invalid transition raises LifecycleError with diagnostic info.
- DT-3: Same-state "transition" is a no-op (allowed, not an error).
- DT-4: All declared transitions in VALID_TRANSITIONS are reachable
  (no orphan entries).
- DT-5: LifecycleError message includes the directive_id when provided.
"""

from __future__ import annotations

import pytest

from secondsight.analysis.schemas import DirectiveStatus
from secondsight.feedback.lifecycle import (
    VALID_TRANSITIONS,
    LifecycleError,
    can_transition,
    reachable_from,
    validate_transition,
)


class TestDeathPaths:
    def test_dt_1_superseded_is_terminal(self) -> None:
        """DT-1: No valid transitions from superseded."""
        assert reachable_from(DirectiveStatus.SUPERSEDED) == frozenset()
        with pytest.raises(LifecycleError):
            validate_transition(
                DirectiveStatus.SUPERSEDED,
                DirectiveStatus.ACTIVE,
            )

    def test_dt_2_invalid_transition_raises_lifecycle_error(self) -> None:
        """DT-2: expired → disabled is not a legal transition."""
        with pytest.raises(LifecycleError) as exc_info:
            validate_transition(
                DirectiveStatus.EXPIRED,
                DirectiveStatus.DISABLED,
            )
        assert "expired" in str(exc_info.value)
        assert "disabled" in str(exc_info.value)

    def test_dt_3_same_state_is_noop(self) -> None:
        """DT-3: Transitioning to the same state does not raise."""
        for status in DirectiveStatus:
            validate_transition(status, status)

    def test_dt_4_no_orphan_transitions(self) -> None:
        """DT-4: Every status in DirectiveStatus has an entry in VALID_TRANSITIONS."""
        for status in DirectiveStatus:
            assert status in VALID_TRANSITIONS

    def test_dt_5_error_includes_directive_id(self) -> None:
        """DT-5: LifecycleError names the directive for diagnostics."""
        with pytest.raises(LifecycleError) as exc_info:
            validate_transition(
                DirectiveStatus.SUPERSEDED,
                DirectiveStatus.ACTIVE,
                directive_id="dir-42",
            )
        assert "dir-42" in str(exc_info.value)


class TestHappyPath:
    @pytest.mark.parametrize(
        "current,target",
        [
            (DirectiveStatus.ACTIVE, DirectiveStatus.DISABLED),
            (DirectiveStatus.ACTIVE, DirectiveStatus.OBSOLETE),
            (DirectiveStatus.ACTIVE, DirectiveStatus.SUPERSEDED),
            (DirectiveStatus.ACTIVE, DirectiveStatus.EXPIRED),
            (DirectiveStatus.DISABLED, DirectiveStatus.ACTIVE),
            (DirectiveStatus.OBSOLETE, DirectiveStatus.ACTIVE),
            (DirectiveStatus.EXPIRED, DirectiveStatus.ACTIVE),
        ],
    )
    def test_valid_transitions(self, current: DirectiveStatus, target: DirectiveStatus) -> None:
        validate_transition(current, target)
        assert can_transition(current, target) is True

    @pytest.mark.parametrize(
        "current,target",
        [
            (DirectiveStatus.DISABLED, DirectiveStatus.OBSOLETE),
            (DirectiveStatus.DISABLED, DirectiveStatus.EXPIRED),
            (DirectiveStatus.OBSOLETE, DirectiveStatus.DISABLED),
            (DirectiveStatus.EXPIRED, DirectiveStatus.SUPERSEDED),
            (DirectiveStatus.SUPERSEDED, DirectiveStatus.ACTIVE),
            (DirectiveStatus.SUPERSEDED, DirectiveStatus.DISABLED),
        ],
    )
    def test_invalid_transitions(self, current: DirectiveStatus, target: DirectiveStatus) -> None:
        assert can_transition(current, target) is False
