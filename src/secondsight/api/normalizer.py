"""Normalizer Protocol, NormalizerRegistry, IdentityNormalizer (P1-5, Task-3).

The Normalizer is the seam between raw hook payloads and the canonical Event
shape. Real adapters (P1-9..P1-11) will land here; for Phase 1 we ship only
IdentityNormalizer for agent="test".

Design assumptions:
- `IdentityNormalizer` requires the envelope's `payload` to already contain
  canonical Event fields (or relies on envelope fields directly). It is only
  for tests and for the initial integration baseline. Production adapters will
  extract real fields from agent-specific payload shapes.
- `NormalizerRegistry` stores normalizers in insertion order. `for_()` calls
  `supports()` on each in order and returns the first match. This means more
  specific normalizers should be registered before more general ones.
- `supports(agent, event_type)` both arguments are required. An agent may
  support only a subset of event types; the registry finds the right normalizer.

Silent failure conditions:
- If no normalizer is registered for a valid (agent, event_type) pair that
  arrives in production, `NoNormalizerError` is raised and the route handler
  returns 422. The event is lost if no fallback JSONL exists. This is correct
  behavior (P1-9..P1-11 fix this by registering real adapters).
- `IdentityNormalizer` silently ignores `payload` dict fields and builds the
  PartialEvent from envelope fields directly. If a test sends a payload with
  extra semantics expecting them to flow through, they will not — `payload`
  is passed as `data` verbatim only.

If the design assumption that 'body.agent wins over header' stops holding,
the NormalizerRegistry lookup key must be updated to consult headers, and the
route handler must pass the header value here. Currently only body.agent is used.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType
from secondsight.observation.tracker import PartialEvent


class NoNormalizerError(Exception):
    """Raised when no registered normalizer supports the given (agent, event_type) pair.

    The message names the missing pair for debuggability.
    """


@runtime_checkable
class Normalizer(Protocol):
    """Protocol that every normalizer must implement.

    The `supports()` method allows the registry to select the right normalizer
    without trying to normalize and catching errors.

    Production note: normalizers are stateless — they do not cache per-session
    state. State belongs to SessionTracker.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        """Return True if this normalizer handles the given (agent, event_type)."""
        ...

    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent:
        """Convert an envelope into a PartialEvent.

        Args:
            envelope: The validated hook envelope from the request body.
            event_type: The event_type string from the URL path (already
                validated against the EventType enum by the route handler).

        Returns:
            A PartialEvent ready for SessionTracker.bind().

        Raises:
            ValueError: If the envelope does not contain required fields for
                this normalizer to produce a PartialEvent.
        """
        ...


class IdentityNormalizer:
    """Stub normalizer for tests and the baseline.

    Supports agent="test" for all canonical EventType values.
    Builds PartialEvent directly from envelope fields; `payload` is passed
    through as `data`. No semantic transformation is applied.

    Real adapters land in P1-9..P1-11 and register themselves alongside
    IdentityNormalizer.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        """Return True only for agent='test' and valid EventType values."""
        if agent != "test":
            return False
        try:
            EventType(event_type)
            return True
        except ValueError:
            return False

    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent:
        """Pass-through: build PartialEvent from envelope fields.

        `envelope.payload` is used as `data` for the PartialEvent. Any
        adapter-specific semantics must be handled by a real normalizer.
        """
        return PartialEvent(
            id=envelope.event_id,
            session_id=envelope.session_id,
            project_id=envelope.project_id,
            # defensive: re-validate in case the Normalizer is invoked outside
            # the route-handler context (where event_type has already been checked
            # against the EventType enum). IdentityNormalizer is a documented
            # test/baseline normalizer; defense-in-depth is appropriate.
            event_type=EventType(event_type),
            timestamp=envelope.timestamp,
            sequence_number=envelope.sequence_number,
            data=dict(envelope.payload),
        )


class NormalizerRegistry:
    """Registry of Normalizer instances, looked up by (agent, event_type).

    Usage:
        registry = NormalizerRegistry()
        registry.register(IdentityNormalizer())
        normalizer = registry.for_("test", "session_start")  # returns IdentityNormalizer

    Design: first-match wins. Register more specific normalizers before
    more general ones.
    """

    def __init__(self) -> None:
        self._normalizers: list[Normalizer] = []

    def register(self, normalizer: Normalizer) -> None:
        """Add a normalizer to the registry."""
        self._normalizers.append(normalizer)

    def for_(self, agent: str, event_type: str) -> Normalizer:
        """Return the first registered normalizer that supports (agent, event_type).

        Raises:
            NoNormalizerError: if no registered normalizer supports the pair.
        """
        for normalizer in self._normalizers:
            if normalizer.supports(agent, event_type):
                return normalizer
        raise NoNormalizerError(
            f"No normalizer registered for (agent={agent!r}, event_type={event_type!r}). "
            f"Registered normalizers: {len(self._normalizers)}. "
            f"Hint: register a normalizer that supports this pair, or check that "
            f"the agent identifier matches the registered normalizer's supports() scope."
        )


__all__ = [
    "IdentityNormalizer",
    "Normalizer",
    "NormalizerRegistry",
    "NoNormalizerError",
]
