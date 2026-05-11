# Task 1: AgentAdapter ABC + AdapterRegistry (P1-9-base)

## Context

Read: `2-plan.md` §1 (translation deltas), §2 (wave structure), §6 (acceptance criteria), §7 (assumptions).

This task creates the adapter contract. It does NOT migrate the existing `Normalizer` Protocol — that's task-3. The two coexist on disk for one wave so reviewers can verify the new ABC matches SD §4.2 *before* migration touches the call sites.

**Plan refs:** P1-9 (interface side)
**SD refs:** §4.2 (with erratum noted in plan §1.1)

## Files

- Create: `src/secondsight/adapters/__init__.py`
- Create: `src/secondsight/adapters/base.py`
- Create: `tests/adapters/__init__.py`
- Create: `tests/adapters/test_base.py`

## Public Contract

```python
# adapters/base.py

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from secondsight.api.schemas import HookEnvelope
from secondsight.observation.tracker import PartialEvent

if TYPE_CHECKING:
    from secondsight.feedback import Convention, Hint  # Phase 2; not yet implemented


class NoAdapterError(Exception):
    """Raised when AdapterRegistry has no adapter for (agent, event_type)."""


class AgentAdapter(ABC):
    """Single source of truth for agent-side observation + feedback.

    SD §4.2 contract. normalize() returns PartialEvent (runtime) — not Event
    as the SD literal text says; see plan §1 erratum.
    """

    @abstractmethod
    def supports(self, agent: str, event_type: str) -> bool:
        """Dispatch query: does this adapter handle (agent, event_type)?"""

    @abstractmethod
    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent:
        """Convert a hook envelope into a PartialEvent ready for SessionTracker.bind()."""

    @abstractmethod
    def supported_event_types(self) -> set[str]:
        """Capability publication: which EventType.value strings this adapter can produce."""

    def inject_convention(self, convention: "Convention") -> str:
        """Phase-2 directive injection. Default: not implemented; subclasses override."""
        raise NotImplementedError(
            "inject_convention is reserved for Phase 2 — see GUR-104"
        )

    def inject_hint(self, hint: "Hint") -> str:
        """Phase-0 reserved per SD §4.2. MUST stay loud-failure."""
        raise NotImplementedError(
            "inject_hint is reserved (Phase 0); see SD §4.2"
        )


class AdapterRegistry:
    """First-match-wins registry. Same dispatch shape as the old NormalizerRegistry."""

    def __init__(self) -> None:
        self._adapters: list[AgentAdapter] = []

    def register(self, adapter: AgentAdapter) -> None: ...
    def for_(self, agent: str, event_type: str) -> AgentAdapter: ...
    def list_supported_event_types(self, agent: str) -> set[str]:
        """Phase-3 dashboard helper. Union of supported_event_types across all adapters that supports() the agent for any event type."""
```

## Death tests (write red BEFORE production code)

DT-1: ABC cannot be instantiated. `AgentAdapter()` → `TypeError`.
DT-2: Subclass missing `normalize` cannot be instantiated. → `TypeError`.
DT-3: `inject_hint` raises `NotImplementedError` with message containing both `"Phase 0"` and `"SD §4.2"`.
DT-4: `inject_convention` raises `NotImplementedError` with message containing `"Phase 2"` and `"GUR-104"`.
DT-5: `AdapterRegistry.for_("nonexistent", "user_prompt")` → `NoAdapterError` whose message names both `"nonexistent"` and `"user_prompt"`.
DT-6: An adapter whose `supports()` returns True but `supported_event_types()` returns empty set is detected by a registry consistency assertion (`for_()` must not return an adapter that contradicts itself). → either `NoAdapterError` or a `RuntimeError` naming the inconsistency.

## Unit tests

- `AdapterRegistry.register` + `for_` happy path with a stub `AgentAdapter` subclass.
- `AdapterRegistry.list_supported_event_types("agent_x")` returns the union of supported types from adapters whose `supports("agent_x", *)` ever returns True.
- A subclass that implements all abstract methods can be instantiated.
- `AdapterRegistry.for_` is first-match-wins (insertion order).

## Implementation steps

- [ ] STEP 0 — answer the four prerequisite questions
- [ ] Write death tests (DT-1..DT-6)
- [ ] Run death tests → red
- [ ] Write unit tests
- [ ] Implement `NoAdapterError`, `AgentAdapter`, `AdapterRegistry`
- [ ] Run all tests → green
- [ ] mypy clean
- [ ] Scar report

## Acceptance for this task

- All death tests + unit tests pass
- mypy clean
- `tests/adapters/__init__.py` exists (else pytest test discovery diverges)
- No imports from `secondsight.api.normalizer` in the new files
- Task-1 scar report committed
