"""Death tests + unit tests for IdentityAdapter (task-3 migration).

This file replaces the IdentityNormalizer tests that previously lived in
`tests/api/test_hooks_endpoint.py`. The unit tests preserve every behavioural
assertion from the prior IdentityNormalizer suite (per AC-3) and add structural
ABC death tests that the duck-typed Protocol predecessor could not enforce.

Death tests:

    DT-1  `import secondsight.api.normalizer` raises ModuleNotFoundError    (AC-1)
          The legacy module is deleted in task-3. If anything resurrects it
          (re-export shim, accidental commit), the silent-rot fallback path
          re-opens. AC-1 must hold for the bundle gate.

    DT-3  isinstance(IdentityAdapter(), AgentAdapter)                       (ABC migration)
          Confirms IdentityAdapter is a true ABC subclass — not a duck-typed
          Protocol holdout. Without this guard, the migration could leave
          IdentityAdapter looking right via duck typing while skipping the
          loud-failure inheritance the ABC contract delivers.

    DT-4  inject_hint / inject_convention loud-failure inherited            (Phase 0/2)
          IdentityAdapter does NOT override the inject_* methods. The ABC
          defaults must surface verbatim — confirms the migration did not
          silently drop the loud-failure contract by adding an override.

Unit tests preserve the IdentityNormalizer behavioural surface:
- supports("test", *) for every EventType value.
- supports("unknown-agent", *) is False.
- supported_event_types() returns the full EventType value set (new — task-3 §3).
- normalize() builds the right PartialEvent from the envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from secondsight.adapters import (
    AdapterRegistry,
    AgentAdapter,
    IdentityAdapter,
    NoAdapterError,
)
from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType
from secondsight.observation.tracker import PartialEvent


# ---------------------------------------------------------------------------
# DEATH TESTS
# ---------------------------------------------------------------------------


def test_dt1_legacy_normalizer_module_is_gone() -> None:
    """DT-1 (AC-1): `import secondsight.api.normalizer` must raise ModuleNotFoundError.

    The migration deleted the module. A passing import means the file came
    back — either as a re-export shim (forbidden by plan G4) or by accident.
    Either way, downstream callers can silently regress to the old Protocol
    path. The grep guard (MIG-DT-1 in test_base.py) catches `from …` imports;
    this guard catches the bare `import …` form which the grep needle misses.
    """
    with pytest.raises(ModuleNotFoundError) as exc_info:
        # mypy's static view sees the package path `secondsight.api.*` and
        # complains via [import-untyped] / [import-not-found] depending on
        # the resolver state. Suppress both — the runtime ModuleNotFoundError
        # is the death-test signal we actually care about.
        import secondsight.api.normalizer  # type: ignore[import-not-found,import-untyped]  # noqa: F401
    assert "secondsight.api.normalizer" in str(exc_info.value), (
        f"DT-1: ModuleNotFoundError must name the deleted module. Got: {exc_info.value!r}"
    )


def test_dt3_identity_adapter_is_agent_adapter_subclass() -> None:
    """DT-3: IdentityAdapter() is an AgentAdapter (ABC subclass, not duck type).

    The pre-migration IdentityNormalizer was a Protocol implementer — `isinstance`
    against the runtime_checkable Protocol returned True for any class with the
    right shape. After migration, IdentityAdapter must be a real ABC subclass so
    the missing-method guards (DT-2 in test_base.py) and inject_* loud-failure
    defaults are inherited rather than re-asserted by every test.
    """
    adapter = IdentityAdapter()
    assert isinstance(adapter, AgentAdapter), (
        "DT-3: IdentityAdapter must subclass AgentAdapter — duck-typed "
        "Protocol holdout would skip ABC enforcement."
    )


def test_dt4_inject_hint_returns_empty_string() -> None:
    """DT-4: IdentityAdapter inherits AgentAdapter.inject_hint pass-through stub (GUR-108, P3B-5).

    inject_hint now returns "" (pass-through stub) instead of raising
    NotImplementedError. The hint engine is reserved for future use.
    """
    adapter = IdentityAdapter()
    result = adapter.inject_hint(object())  # type: ignore[arg-type]
    assert result == "", f"DT-4 inject_hint: should return empty string, got {result!r}"


def test_dt4_inject_convention_loud_failure_is_inherited() -> None:
    """DT-4: IdentityAdapter inherits AgentAdapter.inject_convention loud-failure default.
    IdentityAdapter does NOT override inject_convention (it's a test adapter),
    so the base class NotImplementedError fires with the adapter's class name."""
    adapter = IdentityAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.inject_convention(object())  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "IdentityAdapter" in msg, f"DT-4 inject_convention: missing class name — {msg!r}"
    assert "inject_convention" in msg, f"DT-4 inject_convention: missing method name — {msg!r}"


# ---------------------------------------------------------------------------
# UNIT TESTS — behavioural parity with prior IdentityNormalizer suite (AC-3)
# ---------------------------------------------------------------------------


def test_supports_test_agent_for_every_event_type() -> None:
    """IdentityAdapter.supports('test', et.value) is True for every EventType."""
    adapter = IdentityAdapter()
    for et in EventType:
        assert adapter.supports("test", et.value), (
            f"IdentityAdapter must support ('test', {et.value!r})"
        )


def test_supports_returns_false_for_unknown_agent() -> None:
    """IdentityAdapter.supports() rejects any agent other than 'test'."""
    adapter = IdentityAdapter()
    assert not adapter.supports("claude_code", "user_prompt"), (
        "IdentityAdapter must NOT claim agent='claude_code'"
    )
    assert not adapter.supports("unknown", "session_start"), (
        "IdentityAdapter must NOT claim unknown agents"
    )


def test_supports_returns_false_for_unknown_event_type() -> None:
    """IdentityAdapter.supports('test', not-an-EventType) is False — DT-6 alignment."""
    adapter = IdentityAdapter()
    assert not adapter.supports("test", "not_a_real_event_type"), (
        "IdentityAdapter must reject unknown event_type strings"
    )


def test_supported_event_types_is_full_eventtype_set() -> None:
    """supported_event_types() returns every EventType value (task-3 §3, AC-4 floor)."""
    adapter = IdentityAdapter()
    expected = {e.value for e in EventType}
    assert adapter.supported_event_types() == expected, (
        f"IdentityAdapter must publish the full EventType set; "
        f"got {adapter.supported_event_types()!r}, want {expected!r}"
    )


def test_supports_and_supported_event_types_are_consistent() -> None:
    """For every et in supported_event_types(), supports('test', et) is True (DT-6 echo)."""
    adapter = IdentityAdapter()
    for et in adapter.supported_event_types():
        assert adapter.supports("test", et), (
            f"DT-6 alignment: supported_event_types() includes {et!r} "
            f"but supports('test', {et!r}) is False"
        )


def test_normalize_produces_partial_event_from_envelope() -> None:
    """normalize() forwards envelope fields verbatim into PartialEvent (AC-3 parity)."""
    adapter = IdentityAdapter()
    envelope = HookEnvelope(
        project_id="proj-x",
        session_id="sess-x",
        agent="test",
        event_id="evt-x-001",
        timestamp=datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"some_key": "some_val"},
    )
    partial = adapter.normalize(envelope, "session_start")
    assert isinstance(partial, PartialEvent)
    assert partial.id == "evt-x-001"
    assert partial.session_id == "sess-x"
    assert partial.project_id == "proj-x"
    assert partial.event_type == EventType.SESSION_START
    assert partial.sequence_number == 0
    # Postcondition: payload is copied into data verbatim. The dict() copy
    # in IdentityAdapter prevents downstream mutation of the envelope's payload.
    assert partial.data == {"some_key": "some_val"}
    assert partial.data is not envelope.payload, (
        "IdentityAdapter must copy payload into data, not alias it — alias "
        "would let downstream mutation of Event.data corrupt the envelope."
    )


def test_normalize_rejects_unknown_event_type_string() -> None:
    """normalize('not_a_real_event_type') raises ValueError, not silent default.

    Defense in depth: the route handler validates event_type before calling
    normalize(), but if a future caller bypasses that validation, the adapter
    must surface the error rather than silently constructing a corrupt
    PartialEvent. Mirrors AgentAdapter.normalize postcondition (test_base.py).
    """
    adapter = IdentityAdapter()
    envelope = HookEnvelope(
        project_id="proj-x",
        session_id="sess-x",
        agent="test",
        event_id="evt-x-001",
        timestamp=datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={},
    )
    with pytest.raises(ValueError):
        adapter.normalize(envelope, "not_a_real_event_type")


def test_registry_round_trip_with_identity_adapter() -> None:
    """AdapterRegistry returns the registered IdentityAdapter for ('test', et)."""
    registry = AdapterRegistry()
    adapter = IdentityAdapter()
    registry.register(adapter)
    chosen = registry.for_("test", "session_start")
    assert chosen is adapter


def test_registry_unknown_agent_raises_no_adapter_error() -> None:
    """Registry with only IdentityAdapter raises NoAdapterError for ('claude_code', *)."""
    registry = AdapterRegistry()
    registry.register(IdentityAdapter())
    with pytest.raises(NoAdapterError) as exc_info:
        registry.for_("claude_code", "session_start")
    msg = str(exc_info.value)
    assert "claude_code" in msg, f"Error must name the agent; got {msg!r}"
    assert "session_start" in msg, f"Error must name the event_type; got {msg!r}"
