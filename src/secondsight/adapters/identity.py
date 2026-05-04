"""IdentityAdapter — pass-through adapter for agent='test' (P1-9 migration).

This module replaces `secondsight.api.normalizer.IdentityNormalizer` (deleted
in task-3 of phase1-adapters / GUR-97). The behavioural contract is preserved
so every prior IdentityNormalizer test passes unchanged (AC-3); the structural
shape is upgraded from a duck-typed Protocol implementer to an `AgentAdapter`
ABC subclass so missing-method and silent-default failure modes surface loudly.

Why "Identity" remains a useful production primitive after Phase 1:
    Tests and the agent="test" baseline rely on a normalize() that does not
    impose any agent-specific semantics. Real adapters (ClaudeCodeAdapter,
    Codex, OpenCode, …) own the field-extraction logic for their respective
    payloads; IdentityAdapter exists so a hook server with no real adapter
    registered can still ingest test traffic. AdapterRegistry.for_() iterates
    adapters in insertion order — register real adapters BEFORE IdentityAdapter
    if you want IdentityAdapter to act only as a fallback for agent="test".

Silent failure conditions this module deliberately closes:
    - Returning a PartialEvent with empty / synthesised fields when the
      envelope is malformed: normalize() validates event_type against the
      EventType enum and lets EventType raise ValueError. The route handler
      surfaces ValueError as 422; we do not catch it locally.
    - Aliasing envelope.payload as Event.data: data is constructed via
      `dict(envelope.payload)` so downstream Event.data mutations cannot
      corrupt the envelope (covered by the test_normalize unit test).

Inherited from `AgentAdapter`:
    inject_convention(...)   — raises NotImplementedError("Phase 2 — see GUR-104")
    inject_hint(...)         — raises NotImplementedError("Phase 0 reserved; see SD §4.2")

These defaults are intentionally NOT overridden. Phase 2 (GUR-104) ships the
Convention runtime and either subclasses IdentityAdapter or registers a new
adapter that overrides inject_convention.
"""

from __future__ import annotations

from secondsight.adapters.base import AgentAdapter
from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType
from secondsight.observation.tracker import PartialEvent


class IdentityAdapter(AgentAdapter):
    """Pass-through AgentAdapter for agent='test'.

    Renamed from `IdentityNormalizer` (Protocol) to `IdentityAdapter` (ABC
    subclass) by task-3 of phase1-adapters. Behaviour is preserved verbatim.

    Scope: agent='test' for every value in `EventType`. Unknown agents and
    unknown event_type strings return False from `supports()`. The dispatch
    consistency invariant (DT-6 in test_base.py) holds because
    `supported_event_types()` returns the same EventType set that
    `supports("test", *)` answers True for.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        """Return True only for agent='test' and a recognised EventType value."""
        if agent != "test":
            return False
        try:
            EventType(event_type)
            return True
        except ValueError:
            return False

    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent:
        """Forward envelope fields verbatim into a PartialEvent.

        `envelope.payload` is copied (not aliased) into `data` so downstream
        mutations of Event.data cannot corrupt the envelope's payload.

        Raises:
            ValueError: if `event_type` is not a recognised EventType value.
                The route handler validates event_type before calling
                normalize(), but we re-validate here so this adapter is
                safe to use outside the route-handler context (defence-in-depth
                against future callers that bypass the FastAPI validation).
        """
        return PartialEvent(
            id=envelope.event_id,
            session_id=envelope.session_id,
            project_id=envelope.project_id,
            event_type=EventType(event_type),
            timestamp=envelope.timestamp,
            sequence_number=envelope.sequence_number,
            data=dict(envelope.payload),
        )

    def supported_event_types(self) -> set[str]:
        """Publish every EventType value — the test agent is universal by design.

        DT-6 alignment: `supports("test", et)` returns True for every `et`
        in this set, so the AdapterRegistry consistency guard never rejects
        IdentityAdapter on its own claims.
        """
        return {e.value for e in EventType}


__all__ = ["IdentityAdapter"]
