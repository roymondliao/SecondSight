"""AgentAdapter ABC + AdapterRegistry — SD §4.2 contract (P1-9-base).

This module is the single source of truth for the agent-adapter layer once
task-3 migrates the GUR-96 `Normalizer` Protocol into it. Every concrete
adapter (Claude Code, Codex, OpenCode, …) implements `AgentAdapter` and is
registered via `AdapterRegistry`.

SD §4.2 specifies the canonical 4-method contract:

    normalize(...)              -> PartialEvent  (see plan §1 erratum vs. SD `-> Event`)
    inject_convention(...)      -> str           (Phase 2 — GUR-104)
    inject_hint(...)            -> str           (Phase 0 reserved)
    supported_event_types()     -> set[str]

Plus, retained from GUR-96 because the registry needs runtime dispatch:

    supports(agent, event_type) -> bool          (dispatch query)

Why the SD §4.2 erratum (`-> Event` vs runtime `-> PartialEvent`):
    SessionTracker.bind() owns sequence_number / segment_index /
    sub_agent_id / depth assignment (tracker.py). Adapters cannot return a
    fully-formed `Event` without either (a) placeholder fields overwritten
    downstream (silent state mutation) or (b) calling into the tracker
    (cyclic dependency). Runtime is the source of truth; SD is documentation
    drift, recorded in plan §1.1.

Silent-failure surface this module deliberately closes:
    - inject_hint returning "": tests assert NotImplementedError with
      "Phase 0" + "SD §4.2" in the message (DT-3).
    - inject_convention returning "": same pattern with "Phase 2" + "GUR-104"
      (DT-4).
    - AdapterRegistry.for_() returning a wrong adapter: dispatch consults
      supports() (cheap), but a bonus consistency check verifies the chosen
      adapter publishes the requested event_type via supported_event_types().
      An adapter whose supports() lies relative to its capability set is
      rejected loudly (DT-6) — silent dispatch of the wrong adapter would
      corrupt every Event.data downstream.

Forward references: `Convention` and `Hint` are not yet implemented (Phase 2,
Phase 3+). They are imported under TYPE_CHECKING only so this module does not
depend on unwritten code. Method signatures use string forward references.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from secondsight.api.schemas import IngressEnvelope
from secondsight.observation.tracker import PartialEvent

if TYPE_CHECKING:
    from secondsight.feedback.convention import Convention  # noqa: F401
    from secondsight.feedback.hint import Hint  # noqa: F401


class NoAdapterError(Exception):
    """Raised when AdapterRegistry has no adapter for (agent, event_type).

    The error message names the missing pair so operators can diagnose missing
    adapter registration without grepping logs (DT-5).
    """


class AgentAdapter(ABC):
    """SD §4.2 contract — observation + feedback for a single agent type.

    Concrete adapters subclass this. The three abstract methods MUST be
    implemented (DT-2 enforces). The two `inject_*` methods have loud-failure
    defaults (DT-3, DT-4); subclasses override only when their corresponding
    runtime phase ships.
    """

    @abstractmethod
    def supports(self, agent: str, event_type: str) -> bool:
        """Return True if this adapter handles the (agent, event_type) pair.

        Used by AdapterRegistry.for_() for dispatch. First-match-wins order.
        """

    @abstractmethod
    def normalize(self, envelope: IngressEnvelope, event_type: str) -> PartialEvent:
        """Convert a hook envelope into a PartialEvent for SessionTracker.bind().

        The returned PartialEvent must have id, session_id, project_id,
        event_type, timestamp, sequence_number populated; the tracker fills
        segment_index, sub_agent_id, depth.

        Returns PartialEvent — NOT Event. SD §4.2 reads `-> Event`; that is
        documentation drift recorded in plan §1.1 (see module docstring for
        full rationale). Do not "fix" the return type back to Event without
        first removing SessionTracker.bind() ownership of segment/sequence.

        Postcondition (NOT enforced at this seam — concrete adapters must
        verify in their own death tests; carried forward to task-4 GUR-124):
            ingress-derived fields (id, timestamp, sequence_number) MUST be
            forwarded faithfully from `envelope`; adapter-derived fields
            (session_id, project_id) must be extracted from the raw payload or
            from explicitly provided compatibility fields. A subclass returning
            PartialEvent with empty required fields silently corrupts every
            downstream Event.

        Raises:
            ValueError: envelope is missing required fields for this adapter.
        """

    @abstractmethod
    def supported_event_types(self) -> set[str]:
        """Return the set of EventType.value strings this adapter can produce.

        Capability publication for downstream consumers (dashboards, analysis).
        Distinct from `supports()`: dispatch ↔ capability. The registry
        consistency guard (DT-6) requires that for every (agent, et) where
        supports() is True, et is in supported_event_types().
        """

    def inject_convention(self, convention: "Convention") -> str:
        """Format a convention for system prompt injection.

        Subclasses MUST override. The base raises NotImplementedError so
        adapters that haven't implemented injection fail loudly rather than
        silently returning empty strings.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement inject_convention. "
            "Override in your adapter subclass (see ClaudeCodeAdapter for reference)."
        )

    def inject_hint(self, hint: "Hint") -> str:
        """Pass-through stub per SD §4.2 (GUR-108, P3B-5).

        Returns empty string — hint injection is reserved for future use.
        The HintSelector.match() stub returns [] so this method is never
        called in production. When the hint engine ships, subclasses
        override with real formatting logic.
        """
        return ""


class AdapterRegistry:
    """First-match-wins registry of AgentAdapter instances.

    Dispatch (`for_()`) iterates adapters in insertion order, returns the first
    whose `supports(agent, event_type)` is True AND whose
    `supported_event_types()` contains `event_type`. The second clause is the
    DT-6 consistency guard: an adapter that lies about capability is rejected
    instead of silently producing wrong Events.

    Capability introspection (`list_supported_event_types()`) is for Phase-3
    dashboards — it unions `supported_event_types()` across adapters that
    `supports(agent, *)` for any event_type.
    """

    def __init__(self) -> None:
        self._adapters: list[AgentAdapter] = []

    def register(self, adapter: AgentAdapter) -> None:
        """Append adapter to the registry. Order is significant for first-match.

        Emits a `RuntimeWarning` if the new adapter would shadow an already-
        registered one (same `supports()` answer for any event type in the
        new adapter's `supported_event_types()`). First-match-wins is the
        intentional dispatch model (plan §1 decision 5), but a SILENT shadow
        is the failure mode this warning prevents — operators registering a
        second Claude Code adapter "to take over" must know the first one
        will keep handling traffic until removed.
        """
        new_types = adapter.supported_event_types()
        for existing in self._adapters:
            shadowed = sorted(new_types & existing.supported_event_types())
            if shadowed:
                # Note: this is an event-type overlap warning, not a dispatch
                # collision proof — the two adapters may still gate on different
                # agent names via supports(). We surface the overlap because
                # the silent-shadow failure mode is asymmetric: false positives
                # cost an operator one log line; false negatives corrupt
                # dispatch invisibly until production traffic surfaces them.
                import warnings

                warnings.warn(
                    f"AdapterRegistry.register: new adapter publishes event_types "
                    f"{shadowed!r} that overlap with an already-registered adapter. "
                    f"First-match-wins dispatch will route shared (agent, event_type) "
                    f"pairs to the existing adapter; the new one is shadowed. If "
                    f"both adapters gate on different agent names this is benign — "
                    f"otherwise remove the existing adapter first.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                break  # one warning per register() call is enough; do not spam
        self._adapters.append(adapter)

    def for_(self, agent: str, event_type: str) -> AgentAdapter:
        """Return the first registered adapter that handles (agent, event_type).

        Raises:
            NoAdapterError: no adapter both supports() the pair AND publishes
                event_type in supported_event_types(). The error message names
                the pair so operators can diagnose registration gaps.
        """
        for adapter in self._adapters:
            if not adapter.supports(agent, event_type):
                continue
            if event_type not in adapter.supported_event_types():
                # DT-6: adapter claims dispatch but does not publish capability.
                # Skip — do NOT silently route a hook through a self-contradicting
                # adapter. Fall through to next adapter, then to NoAdapterError if
                # no honest adapter exists.
                continue
            return adapter
        raise NoAdapterError(
            f"No adapter registered for (agent={agent!r}, event_type={event_type!r}). "
            f"Either no registered adapter supports() this pair, or every adapter "
            f"that does omits {event_type!r} from supported_event_types() "
            f"(consistency guard). Registered adapters: {len(self._adapters)}."
        )

    def list_supported_event_types(self, agent: str) -> set[str]:
        """Union of supported_event_types() across adapters that handle this agent.

        An adapter is considered to "handle" the agent if `supports(agent, et)`
        returns True for at least one event type in its `supported_event_types()`.
        Phase-3 dashboard helper.
        """
        union: set[str] = set()
        for adapter in self._adapters:
            published = adapter.supported_event_types()
            if any(adapter.supports(agent, et) for et in published):
                union |= published
        return union


__all__ = [
    "AdapterRegistry",
    "AgentAdapter",
    "NoAdapterError",
]
