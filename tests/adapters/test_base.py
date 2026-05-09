"""Death tests + unit tests for AgentAdapter ABC + AdapterRegistry (task-1).

Death tests run BEFORE unit tests in this file because they define the silent-
failure surface this task is preventing:

    DT-1  AgentAdapter()                 -> TypeError                     (ABC instantiation guard)
    DT-2  subclass missing normalize     -> TypeError                     (missing-method guard)
    DT-3  adapter.inject_hint(h)         -> NotImplementedError           (Phase 0 reserved + SD §4.2)
    DT-4  adapter.inject_convention(c)   -> NotImplementedError           (Phase 2 reserved + GUR-104)
    DT-5  registry.for_("x", "y")        -> NoAdapterError naming pair    (unknown agent)
    DT-6  supports() ↔ supported_event_types() skew detected at for_()    (consistency guard)

Migration-level death test (task-3, plan §4 death case #1):

    MIG-DT-1  No production source imports `secondsight.api.normalizer`    (AC-10)

    Walks src/ at collect time and asserts zero `from secondsight.api.normalizer`
    hits. The module is deleted in task-3; this guard prevents future re-introduction
    of the legacy import path. A regression here means a stale import survived
    a refactor and the silent-rot path is open again.

If any death test goes green by accident (e.g. silent default added later), the
test must fail loudly so the regression cannot be merged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from secondsight.adapters.base import (
    AdapterRegistry,
    AgentAdapter,
    NoAdapterError,
)
from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType
from secondsight.observation.tracker import PartialEvent


# ---------------------------------------------------------------------------
# MIG-DT-1: Collect-time guard — no production import of legacy normalizer path
# ---------------------------------------------------------------------------
# Runs at module-collection time (NOT inside a test function) so it fails the
# whole pytest session before any other test runs. This is the strongest signal
# we can give: "the migration regressed; do not proceed."
#
# Walks every .py file under src/ and rejects any line that imports from
# `secondsight.api.normalizer`. We intentionally read raw text rather than
# attempting an AST walk: the legacy module is deleted in task-3, so even an
# `if False: from secondsight.api.normalizer import …` (which AST analysis
# might judge harmless) would still fail at import-time on Python 3.14.
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_LEGACY_IMPORT_NEEDLE = "from secondsight.api.normalizer"


def _scan_legacy_imports() -> list[tuple[Path, int, str]]:
    """Return (path, line_no, line) tuples for every legacy import found."""
    hits: list[tuple[Path, int, str]] = []
    for path in _SRC_ROOT.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _LEGACY_IMPORT_NEEDLE in line:
                hits.append((path, line_no, line.strip()))
    return hits


_LEGACY_IMPORT_HITS = _scan_legacy_imports()
if _LEGACY_IMPORT_HITS:
    # pytest.fail at collect time aborts the whole session — exactly what we
    # want for a migration regression. The message names every offender so an
    # operator can reproduce the AC-10 failure without re-running grep.
    _details = "\n".join(f"  {p}:{ln}: {line}" for p, ln, line in _LEGACY_IMPORT_HITS)
    raise pytest.UsageError(
        "MIG-DT-1 (AC-10) regression: production source imports the legacy "
        "`secondsight.api.normalizer` path which was deleted in task-3 of "
        "phase1-adapters. Replace with `from secondsight.adapters import …`.\n"
        f"Offenders:\n{_details}"
    )


# ---------------------------------------------------------------------------
# Test fixtures: minimal subclasses + envelope factory
# ---------------------------------------------------------------------------


def _make_envelope(agent: str = "stub", event_type: str = "user_prompt") -> HookEnvelope:
    return HookEnvelope(
        project_id="proj-1",
        session_id="sess-1",
        agent=agent,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"event_type": event_type},
    )


class _StubAdapter(AgentAdapter):
    """Minimal correct subclass — used by happy-path unit tests."""

    def __init__(self, agent_name: str = "stub", types: set[str] | None = None) -> None:
        self._agent = agent_name
        self._types = types if types is not None else {EventType.USER_PROMPT.value}

    def supports(self, agent: str, event_type: str) -> bool:
        return agent == self._agent and event_type in self._types

    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent:
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
        return set(self._types)


class _DishonestAdapter(AgentAdapter):
    """supports() lies relative to supported_event_types() — drives DT-6.

    Test invariant: `normalize()` is never reached. The registry's DT-6
    consistency guard must reject this adapter at `for_()` before any caller
    can invoke `normalize()`. If the body ever runs, DT-6 has regressed.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        return agent == "dishonest"  # claims to handle anything

    def normalize(
        self, envelope: HookEnvelope, event_type: str
    ) -> PartialEvent:  # pragma: no cover - never reached if DT-6 enforces
        pytest.fail(
            "_DishonestAdapter.normalize() was reached — DT-6 registry consistency "
            "guard regressed. AdapterRegistry.for_() must skip adapters whose "
            "supports() returns True but supported_event_types() omits the type."
        )

    def supported_event_types(self) -> set[str]:
        return set()  # publishes nothing — contradicts supports()


# ---------------------------------------------------------------------------
# DEATH TESTS (DT-1..DT-6)
# ---------------------------------------------------------------------------


def test_dt1_abc_cannot_be_instantiated() -> None:
    """DT-1: AgentAdapter() raises TypeError — abstract base."""
    with pytest.raises(TypeError):
        AgentAdapter()  # type: ignore[abstract]


def test_dt2_subclass_missing_normalize_cannot_be_instantiated() -> None:
    """DT-2: subclass omitting `normalize` is still abstract — TypeError on instantiation."""

    class _Incomplete(AgentAdapter):
        def supports(self, agent: str, event_type: str) -> bool:
            return False

        def supported_event_types(self) -> set[str]:
            return set()

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


def test_dt3_inject_hint_raises_with_required_phrases() -> None:
    """DT-3: inject_hint loud-failure default carries SD §4.2 + Phase 0 reference.

    Method-locality: also asserts that inject_hint's message identifies inject_hint
    specifically (not "Phase 2" / "GUR-104" which would mean inject_convention's
    body ran instead). Catches the regression where a future ABC refactor swaps
    the bodies of the two methods.
    """
    adapter = _StubAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.inject_hint(object())  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "Phase 0" in msg, f"DT-3: missing 'Phase 0' guard — message was {msg!r}"
    assert "SD §4.2" in msg, f"DT-3: missing 'SD §4.2' reference — message was {msg!r}"
    # Method-locality guard: the message must NOT be inject_convention's message.
    assert "Phase 2" not in msg, (
        f"DT-3: message looks like inject_convention's — wrong method body ran? {msg!r}"
    )
    assert "GUR-104" not in msg, (
        f"DT-3: message looks like inject_convention's — wrong method body ran? {msg!r}"
    )


def test_dt4_inject_convention_raises_with_required_phrases() -> None:
    """DT-4: inject_convention loud-failure default names the adapter class and
    references ClaudeCodeAdapter for implementation guidance.

    Method-locality: also asserts the message is NOT inject_hint's. See DT-3 docstring.
    """
    adapter = _StubAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.inject_convention(object())  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "_StubAdapter" in msg, f"DT-4: missing adapter class name — message was {msg!r}"
    assert "inject_convention" in msg, f"DT-4: missing method name — message was {msg!r}"
    # Method-locality guard: the message must NOT be inject_hint's message.
    assert "Phase 0" not in msg, (
        f"DT-4: message looks like inject_hint's — wrong method body ran? {msg!r}"
    )
    assert "SD §4.2" not in msg, (
        f"DT-4: message looks like inject_hint's — wrong method body ran? {msg!r}"
    )


def test_dt5_unknown_agent_raises_naming_the_pair() -> None:
    """DT-5: registry.for_() with no matching adapter raises NoAdapterError naming the missing pair."""
    registry = AdapterRegistry()
    registry.register(_StubAdapter(agent_name="stub"))
    with pytest.raises(NoAdapterError) as exc_info:
        registry.for_("nonexistent", "user_prompt")
    msg = str(exc_info.value)
    assert "nonexistent" in msg, f"DT-5: error must name unknown agent — was {msg!r}"
    assert "user_prompt" in msg, f"DT-5: error must name unmatched event_type — was {msg!r}"


def test_dt6_supports_supported_event_types_skew_detected() -> None:
    """DT-6: registry rejects an adapter that supports() True but supported_event_types() omits the type.

    A `supports()`-only "yes" without capability publication is a silent-rot path:
    dispatch claims to handle the pair while the published capability set says no.
    Registry's for_() must reject this so the inconsistency surfaces as a typed error,
    not as a wrong-adapter normalization downstream.
    """
    registry = AdapterRegistry()
    registry.register(_DishonestAdapter())
    with pytest.raises((NoAdapterError, RuntimeError)) as exc_info:
        registry.for_("dishonest", "user_prompt")
    msg = str(exc_info.value)
    # The error must mention the inconsistency or the missing pair so an operator
    # can debug. At minimum the event_type that wasn't published.
    assert "user_prompt" in msg, f"DT-6: error must surface the missing event_type — was {msg!r}"


# ---------------------------------------------------------------------------
# UNIT TESTS (happy paths, registry semantics)
# ---------------------------------------------------------------------------


def test_complete_subclass_can_be_instantiated() -> None:
    """A subclass implementing supports + normalize + supported_event_types instantiates fine."""
    adapter = _StubAdapter()
    assert isinstance(adapter, AgentAdapter)


def test_registry_register_and_for_happy_path() -> None:
    """register() + for_() round-trip returns the adapter that supports() the pair."""
    registry = AdapterRegistry()
    a = _StubAdapter(agent_name="agent_a", types={EventType.USER_PROMPT.value})
    b = _StubAdapter(agent_name="agent_b", types={EventType.SESSION_START.value})
    registry.register(a)
    registry.register(b)
    assert registry.for_("agent_a", "user_prompt") is a
    assert registry.for_("agent_b", "session_start") is b


def test_registry_for_first_match_wins_in_insertion_order() -> None:
    """Two adapters that both supports() the same pair: first-registered wins.

    The shadowing warning is an expected side effect of registering an
    overlapping second adapter — it is asserted in test_register_warns_on_shadowing_overlap
    above. Suppress here to keep this test's output focused on dispatch order.
    """
    import warnings

    registry = AdapterRegistry()
    first = _StubAdapter(agent_name="shared", types={EventType.USER_PROMPT.value})
    second = _StubAdapter(agent_name="shared", types={EventType.USER_PROMPT.value})
    registry.register(first)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        registry.register(second)
    chosen = registry.for_("shared", "user_prompt")
    assert chosen is first, "registry must return the first-registered adapter"


def test_registry_list_supported_event_types_unions_matching_adapters() -> None:
    """list_supported_event_types(agent) unions supported_event_types across adapters that ever supports() that agent."""
    registry = AdapterRegistry()
    a = _StubAdapter(agent_name="multi", types={EventType.USER_PROMPT.value})
    b = _StubAdapter(
        agent_name="multi", types={EventType.SESSION_START.value, EventType.SESSION_END.value}
    )
    other = _StubAdapter(agent_name="other", types={EventType.TOOL_USE_START.value})
    registry.register(a)
    registry.register(b)
    registry.register(other)

    multi_types = registry.list_supported_event_types("multi")
    assert multi_types == {
        EventType.USER_PROMPT.value,
        EventType.SESSION_START.value,
        EventType.SESSION_END.value,
    }
    # `other` is excluded because its supports("multi", *) is always False.
    assert EventType.TOOL_USE_START.value not in multi_types


def test_registry_list_supported_event_types_empty_when_no_match() -> None:
    """list_supported_event_types returns empty set for agents no adapter supports()."""
    registry = AdapterRegistry()
    registry.register(_StubAdapter(agent_name="known"))
    assert registry.list_supported_event_types("unknown") == set()


def test_register_warns_on_shadowing_overlap() -> None:
    """DT-7: register() emits RuntimeWarning when new adapter's published types overlap an existing one.

    First-match-wins is the intended dispatch model, but a SILENT shadow is the
    failure mode. Operators registering a "replacement" adapter must see the
    warning so they know dispatch still routes to the original.
    """
    import warnings

    registry = AdapterRegistry()
    registry.register(_StubAdapter(agent_name="primary", types={EventType.USER_PROMPT.value}))

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        registry.register(_StubAdapter(agent_name="primary", types={EventType.USER_PROMPT.value}))

    runtime_warnings = [w for w in captured if issubclass(w.category, RuntimeWarning)]
    assert runtime_warnings, (
        "register() must warn when shadowing an existing adapter's published types"
    )
    assert "user_prompt" in str(runtime_warnings[0].message), (
        f"shadow warning must name the conflicting event_type — got {runtime_warnings[0].message!r}"
    )


def test_register_does_not_warn_on_disjoint_types() -> None:
    """register() emits NO warning when new adapter's published types are disjoint from existing."""
    import warnings

    registry = AdapterRegistry()
    registry.register(_StubAdapter(agent_name="a", types={EventType.USER_PROMPT.value}))

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        registry.register(_StubAdapter(agent_name="b", types={EventType.SESSION_END.value}))

    runtime_warnings = [w for w in captured if issubclass(w.category, RuntimeWarning)]
    assert not runtime_warnings, (
        f"register() must NOT warn on disjoint event_types — got {[str(w.message) for w in runtime_warnings]}"
    )


def test_registered_adapter_normalize_round_trip() -> None:
    """Sanity: a stub adapter retrieved via for_() normalizes correctly."""
    registry = AdapterRegistry()
    registry.register(_StubAdapter(agent_name="stub", types={EventType.USER_PROMPT.value}))
    adapter = registry.for_("stub", "user_prompt")
    envelope = _make_envelope(agent="stub", event_type="user_prompt")
    partial = adapter.normalize(envelope, "user_prompt")
    assert partial.session_id == "sess-1"
    assert partial.event_type == EventType.USER_PROMPT
