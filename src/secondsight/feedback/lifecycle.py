"""Directive lifecycle state machine (GUR-105, P3A-4).

State graph (SD §5.9.1):

    active ──────┬──→ disabled     (user soft-disable via dashboard)
                 ├──→ obsolete     (effectiveness tracking: flag frequency → 0)
                 ├──→ superseded   (semantic dedup: newer convention replaces)
                 ├──→ expired      (TTL expiry enforcement)
                 └──→ stalled      (revision cap reached)

    disabled ────┬──→ active       (user re-enable via dashboard)
                 └──  (terminal for analyzer — only user can re-enable)

    obsolete ────┬──→ active       (re-activation: flag frequency rebounds)
                 └──  (analyzer may re-activate)

    superseded ──┬──  (terminal — the replacement convention is active)
                 └──  (no valid transitions out)

    expired ─────┬──→ active       (re-evaluation finds relevance)
                 └──  (analyzer may re-activate after TTL re-assessment)

    stalled ─────┬──→ active       (same identity re-promoted later)
                 └──  (analyzer may keep it dormant until then)

Transition actors:
    - USER: active ↔ disabled (via PATCH /api/directives/{id})
    - ANALYZER: active → obsolete, active → superseded, active → expired
    - ANALYZER: obsolete → active, expired → active (re-activation)

Silent failure conditions:
    - Requesting a transition not in VALID_TRANSITIONS raises ValueError.
      This is loud by design. A silent no-op would mask lifecycle bugs in
      the aggregator that believe they transitioned a directive.
    - The state machine does NOT enforce reason/disabled_at invariants.
      Those are DirectivesRepository's responsibility (separation of
      concerns: state machine validates the graph, repository validates
      the row invariants).

Design assumptions:
    - `superseded` is terminal. A superseded convention was replaced by a
      better one; re-activating it would create a semantic duplicate. If
      the replacement is later disabled, the original stays superseded.
    - State machine is stateless: it validates (current, target) pairs.
      It does NOT hold a reference to a specific directive or mutate
      anything. Callers (repository, aggregator) own the mutation.

Ref: SD §5.9.1, §5.9.3, §5.9.4
"""

from __future__ import annotations

from secondsight.analysis.schemas import DirectiveStatus

VALID_TRANSITIONS: dict[DirectiveStatus, frozenset[DirectiveStatus]] = {
    DirectiveStatus.ACTIVE: frozenset(
        {
            DirectiveStatus.DISABLED,
            DirectiveStatus.OBSOLETE,
            DirectiveStatus.SUPERSEDED,
            DirectiveStatus.EXPIRED,
            DirectiveStatus.STALLED,
        }
    ),
    DirectiveStatus.DISABLED: frozenset(
        {
            DirectiveStatus.ACTIVE,
        }
    ),
    DirectiveStatus.OBSOLETE: frozenset(
        {
            DirectiveStatus.ACTIVE,
        }
    ),
    DirectiveStatus.SUPERSEDED: frozenset(),
    DirectiveStatus.EXPIRED: frozenset(
        {
            DirectiveStatus.ACTIVE,
        }
    ),
    DirectiveStatus.STALLED: frozenset(
        {
            DirectiveStatus.ACTIVE,
        }
    ),
}


class LifecycleError(ValueError):
    """Raised when a requested lifecycle transition is invalid."""

    def __init__(
        self,
        current: DirectiveStatus,
        target: DirectiveStatus,
        *,
        directive_id: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        self.directive_id = directive_id
        allowed = VALID_TRANSITIONS.get(current, frozenset())
        id_ctx = f" (directive_id={directive_id!r})" if directive_id else ""
        super().__init__(
            f"Invalid lifecycle transition: {current.value!r} → "
            f"{target.value!r}{id_ctx}. "
            f"Valid targets from {current.value!r}: "
            f"{sorted(s.value for s in allowed) or '(none — terminal state)'}."
        )


def validate_transition(
    current: DirectiveStatus,
    target: DirectiveStatus,
    *,
    directive_id: str | None = None,
) -> None:
    """Validate that (current → target) is a legal lifecycle transition.

    Raises:
        LifecycleError: transition not in VALID_TRANSITIONS graph.
    """
    if current == target:
        return
    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None:
        raise LifecycleError(current, target, directive_id=directive_id)
    if target not in allowed:
        raise LifecycleError(current, target, directive_id=directive_id)


def can_transition(current: DirectiveStatus, target: DirectiveStatus) -> bool:
    """Check if (current → target) is valid without raising."""
    if current == target:
        return True
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    return target in allowed


def reachable_from(status: DirectiveStatus) -> frozenset[DirectiveStatus]:
    """Return the set of statuses reachable from the given status."""
    return VALID_TRANSITIONS.get(status, frozenset())


__all__ = [
    "LifecycleError",
    "VALID_TRANSITIONS",
    "can_transition",
    "reachable_from",
    "validate_transition",
]
