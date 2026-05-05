"""Death tests + unit tests for ClaudeCodeAdapter (task-4, P1-10).

Death tests run BEFORE unit tests because they define the silent-failure
surface this task is preventing:

    DT-1  supports("nonexistent", *)              -> False              (unknown agent)
    DT-2  supports("claude_code", "<unknown>")    -> False              (unknown event_type)
    DT-3  privacy canary deep-search per fixture                        (canary leak guard)
    DT-4  envelope.session_id empty               -> ValueError         (envelope-level)
    DT-5  payload missing hook_event_name         -> ValueError         (payload-level)
    DT-6  DROP_LIST raw values absent from data per fixture             (generalised canary)
    DT-7  inject_hint loud-failure inherited      -> NotImplementedError (override regression guard)
    DT-7b inject_convention loud-failure inherited                      (symmetric guard)
    DT-8  supports() ↔ supported_event_types()    consistent            (skew guard)

If any death test goes green by accident (e.g. silent privacy-leak fix), the
test must fail loudly so the regression cannot be merged.

Per-fixture round-trip (AC-5) lives below the death tests; the postcondition
carry-forward from task-1 (envelope-derived fields forwarded faithfully) is
verified on the user_prompt_submit fixture as the simplest reachable path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from secondsight.adapters.claude_code import (
    DROP_LIST,
    ClaudeCodeAdapter,
)
from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType


_AGENT_NAME = "claude_code"

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code"
PRIVACY_CANARY_VALUE = "PRIVACY_CANARY_DO_NOT_STORE"

# P1 floor — must match changes/2026-05-04_phase1-adapters/2-plan.md §7 G1.
P1_FLOOR_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.SESSION_START.value,
        EventType.USER_PROMPT.value,
        EventType.TOOL_USE_START.value,
        EventType.TOOL_USE_END.value,
        EventType.SESSION_END.value,
    }
)


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _load_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _envelope_from_fixture(fixture: Mapping[str, Any]) -> HookEnvelope:
    """Build a HookEnvelope around a fixture's payload.

    The fixture stores the raw Claude Code hook stdin under `payload`; envelope-
    level fields (project_id, agent, event_id, sequence_number, timestamp) are
    synthesised here because they are wire-protocol concerns owned by the bash
    hook script, not by the adapter under test. session_id is sourced from the
    fixture's payload.session_id when present (so session-event fixtures whose
    canary lives at session_id propagate that value into the envelope column —
    DT-3 then verifies it does NOT also reach data).
    """
    payload = dict(fixture["payload"])
    return HookEnvelope(
        project_id="test-proj",
        session_id=str(payload.get("session_id", "test-sess")),
        agent=_AGENT_NAME,
        event_id="test-evt-1",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload=payload,
    )


def _resolve_path(payload: Any, dotted_path: str) -> Any:
    """Resolve a dotted path; return None on any missing/non-dict segment."""
    cursor: Any = payload
    for segment in dotted_path.split("."):
        if not isinstance(cursor, Mapping):
            return None
        if segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


# ---------------------------------------------------------------------------
# DEATH TESTS (DT-1..DT-8)
# ---------------------------------------------------------------------------


def test_dt1_unknown_agent_returns_false() -> None:
    """DT-1: supports() returns False for any agent != 'claude_code'.

    Includes a kebab-case ('claude-code') and case-variant probe — the canonical
    name per plan §7 G3 is snake_case; aliasing is rejected at the dispatch
    seam so misconfigured callers fail loudly instead of silently routing to
    the wrong adapter.
    """
    adapter = ClaudeCodeAdapter()
    assert adapter.supports("nonexistent", EventType.USER_PROMPT.value) is False
    assert adapter.supports("test", EventType.USER_PROMPT.value) is False
    assert adapter.supports("claude-code", EventType.USER_PROMPT.value) is False
    assert adapter.supports("Claude_Code", EventType.USER_PROMPT.value) is False
    assert adapter.supports("", EventType.USER_PROMPT.value) is False


def test_dt2_unknown_event_type_returns_false() -> None:
    """DT-2: supports('claude_code', '<unknown or out-of-floor>') returns False.

    Catches event-type typos AND out-of-floor types (plan §8 lists 'thinking',
    'sub_agent_*' as deferred) silently dispatching to ClaudeCodeAdapter.
    """
    adapter = ClaudeCodeAdapter()
    assert adapter.supports(_AGENT_NAME, "blarg") is False
    assert adapter.supports(_AGENT_NAME, "") is False
    assert adapter.supports(_AGENT_NAME, EventType.THINKING.value) is False
    assert adapter.supports(_AGENT_NAME, EventType.SUB_AGENT_START.value) is False
    assert adapter.supports(_AGENT_NAME, EventType.RESPONSE.value) is False


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt3_privacy_canary_absent_from_data(path: Path) -> None:
    """DT-3 (AC-6): every fixture's canary value MUST NOT appear in
    PartialEvent.data JSON.

    Deep search via JSON serialisation — substring match against every
    string value in the data dict (nested or otherwise). A regression that
    copies tool_input.command (or session_id, or prompt) raw into data
    surfaces here as the canary string leaking through.
    """
    fixture = _load_fixture(path)
    event_type = fixture["_meta"]["_secondsight_event_type"]
    envelope = _envelope_from_fixture(fixture)

    adapter = ClaudeCodeAdapter()
    partial = adapter.normalize(envelope, event_type)

    data_json = json.dumps(partial.data, default=str)
    assert PRIVACY_CANARY_VALUE not in data_json, (
        f"{path.name}: canary value leaked into PartialEvent.data — data JSON: {data_json!r}"
    )


def test_dt4_envelope_missing_session_id_raises() -> None:
    """DT-4: envelope with empty session_id raises ValueError naming session_id.

    Constructed via HookEnvelope.model_construct() to bypass Pydantic's
    min_length=1 validation (which would 422 at the API boundary). The
    adapter's defence-in-depth check ensures this fails loudly even if the
    API contract relaxes — a Pydantic v3 default-coercion change or a future
    `extra='allow'` schema relaxation must not let an empty session_id through.
    """
    envelope = HookEnvelope.model_construct(
        project_id="test-proj",
        session_id="",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.normalize(envelope, EventType.USER_PROMPT.value)
    assert "session_id" in str(exc_info.value), (
        f"DT-4: error must name session_id — got {exc_info.value!r}"
    )


def test_dt5_payload_missing_hook_event_name_raises() -> None:
    """DT-5: payload missing hook_event_name raises ValueError naming the field.

    `hook_event_name` is the Claude Code hook stdin field that names which hook
    fired. The adapter dispatches by caller-supplied `event_type` (route
    parameter) AND cross-checks `hook_event_name` to detect a misrouted POST
    (e.g. POST /hook/tool_use_start carrying a SessionEnd body). Missing it
    means we cannot tell whether the payload truly is a Claude Code hook —
    refuse to silently normalise.
    """
    envelope = HookEnvelope(
        project_id="test-proj",
        session_id="sess-1",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"tool_name": "Read"},
    )
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.normalize(envelope, EventType.TOOL_USE_START.value)
    assert "hook_event_name" in str(exc_info.value), (
        f"DT-5: error must name hook_event_name — got {exc_info.value!r}"
    )


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt6_drop_list_raw_values_absent_from_data(path: Path) -> None:
    """DT-6: every DROP_LIST source path's raw value (whatever it is in the
    fixture's payload) MUST NOT appear in PartialEvent.data JSON.

    Generalised canary — even non-canary sentinels at drop-listed paths must
    not leak. Catches a regression where the canary string is replaced with
    a real value (e.g. `tool_input.command = "echo hi"` in post_tool_use's
    fixture, length 7) and the drop logic still copies it through. The DT-3
    canary alone would not catch this case because the leaked string would
    not equal `PRIVACY_CANARY_DO_NOT_STORE`.
    """
    fixture = _load_fixture(path)
    event_type = fixture["_meta"]["_secondsight_event_type"]
    envelope = _envelope_from_fixture(fixture)

    adapter = ClaudeCodeAdapter()
    partial = adapter.normalize(envelope, event_type)
    data_json = json.dumps(partial.data, default=str)

    payload = fixture["payload"]
    for source_path in DROP_LIST:
        raw = _resolve_path(payload, source_path)
        if raw is None:
            continue
        raw_str = str(raw)
        if not raw_str:
            continue
        assert raw_str not in data_json, (
            f"{path.name}: DROP_LIST path {source_path!r} raw value "
            f"({raw_str!r}) leaked into PartialEvent.data JSON. "
            f"data: {data_json!r}"
        )


def test_dt7_inject_hint_loud_failure_inherited() -> None:
    """DT-7: ClaudeCodeAdapter inherits inject_hint's NotImplementedError.

    Guards against an override regression where a future maintainer overrides
    inject_hint with `return ""` or `pass` — the silent-default failure mode
    the ABC's NotImplementedError exists to prevent. Method-locality assertion
    (Phase 0 / SD §4.2 phrases) prevents accidental swap with inject_convention.
    """
    adapter = ClaudeCodeAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.inject_hint(object())  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "Phase 0" in msg, f"DT-7: missing 'Phase 0' guard — was {msg!r}"
    assert "SD §4.2" in msg, f"DT-7: missing 'SD §4.2' reference — was {msg!r}"


def test_dt7b_inject_convention_loud_failure_inherited() -> None:
    """DT-7b (AC-7): ClaudeCodeAdapter inherits inject_convention's
    NotImplementedError. Symmetric with DT-7 — both inject_* methods are
    independent override seams."""
    adapter = ClaudeCodeAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.inject_convention(object())  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "Phase 2" in msg, f"DT-7b: missing 'Phase 2' guard — was {msg!r}"
    assert "GUR-104" in msg, f"DT-7b: missing 'GUR-104' reference — was {msg!r}"


def test_dt8_supports_consistency_with_supported_event_types() -> None:
    """DT-8: supports('claude_code', et) iff et in supported_event_types().

    Catches the case where one is updated and the other isn't — e.g. a future
    maintainer adds EventType.THINKING to `_HOOK_TO_EVENT_TYPE` (which feeds
    supported_event_types) but forgets to make supports() honour it.
    AdapterRegistry's consistency guard catches dispatch-time skew, but
    capability publication for downstream consumers (dashboards, analysis)
    needs a direct symmetric check.
    """
    adapter = ClaudeCodeAdapter()
    published = adapter.supported_event_types()
    for et in published:
        assert adapter.supports(_AGENT_NAME, et), (
            f"DT-8: et={et!r} in supported_event_types() but supports() returns False"
        )
    for et in {e.value for e in EventType}:
        if et in published:
            continue
        assert not adapter.supports(_AGENT_NAME, et), (
            f"DT-8: supports() True for et={et!r} not in supported_event_types()"
        )


def test_dt9_data_builders_keys_match_hook_event_types() -> None:
    """DT-9: _DATA_BUILDERS keys MUST equal _HOOK_TO_EVENT_TYPE values.

    The module-scope assert fires at import; this test makes the invariant
    explicit in the suite so regressions are visible in CI rather than only
    at server startup. A developer adding a new event type must update BOTH
    tables; this test (plus the assert) closes two detection windows: import-
    time and test-time.
    """
    from secondsight.adapters.claude_code import _DATA_BUILDERS, _HOOK_TO_EVENT_TYPE

    assert set(_HOOK_TO_EVENT_TYPE.values()) == set(_DATA_BUILDERS.keys()), (
        f"_DATA_BUILDERS / _HOOK_TO_EVENT_TYPE divergence: "
        f"unpaired in hook map: {set(_HOOK_TO_EVENT_TYPE.values()) - set(_DATA_BUILDERS.keys())!r}; "
        f"unpaired in builders: {set(_DATA_BUILDERS.keys()) - set(_HOOK_TO_EVENT_TYPE.values())!r}"
    )


# ---------------------------------------------------------------------------
# UNIT TESTS
# ---------------------------------------------------------------------------


def test_supported_event_types_floor() -> None:
    """AC-4: supported_event_types() is a superset of the P1 floor."""
    adapter = ClaudeCodeAdapter()
    assert P1_FLOOR_EVENT_TYPES <= adapter.supported_event_types(), (
        f"P1 floor not covered: missing {P1_FLOOR_EVENT_TYPES - adapter.supported_event_types()}"
    )


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_fixture_round_trip(path: Path) -> None:
    """AC-5: per fixture, normalize().data deep-equals expected_partial_event_data.

    The fixture's `expected_partial_event_data` is the source of truth for the
    drop_list outcome — fixture authoring rules in fixtures/claude_code/_README.md
    bind it to plan §5. This test fails red if either (a) the adapter regresses
    or (b) the fixture is edited inconsistently with the drop_list, both of
    which are surfacing-worthy.
    """
    fixture = _load_fixture(path)
    event_type = fixture["_meta"]["_secondsight_event_type"]
    expected = fixture["expected_partial_event_data"]
    envelope = _envelope_from_fixture(fixture)

    adapter = ClaudeCodeAdapter()
    partial = adapter.normalize(envelope, event_type)
    assert partial.data == expected, (
        f"{path.name}: normalize().data mismatch.\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {partial.data!r}"
    )


def test_normalize_envelope_fields_forwarded() -> None:
    """Postcondition (carried forward from task-1 scar): envelope-derived
    fields (id, session_id, project_id, timestamp, sequence_number) are
    forwarded faithfully — not synthesised as empty strings or zeros.
    """
    fixture = _load_fixture(FIXTURE_DIR / "user_prompt_submit.json")
    envelope = _envelope_from_fixture(fixture)
    adapter = ClaudeCodeAdapter()
    partial = adapter.normalize(envelope, EventType.USER_PROMPT.value)
    assert partial.id == envelope.event_id
    assert partial.session_id == envelope.session_id
    assert partial.project_id == envelope.project_id
    assert partial.event_type == EventType.USER_PROMPT
    assert partial.timestamp == envelope.timestamp
    assert partial.sequence_number == envelope.sequence_number


def test_session_id_routes_to_column_not_data() -> None:
    """SD §3.7.5 invariant: session_id appears on PartialEvent.session_id
    (column) but never inside data. Session-event canary placement
    (fixtures/claude_code/_README.md "Session-event canary rationale")
    relies on this."""
    fixture = _load_fixture(FIXTURE_DIR / "session_start.json")
    envelope = _envelope_from_fixture(fixture)
    adapter = ClaudeCodeAdapter()
    partial = adapter.normalize(envelope, EventType.SESSION_START.value)
    assert partial.session_id == envelope.session_id, "session_id must populate the column"
    data_json = json.dumps(partial.data, default=str)
    assert envelope.session_id not in data_json, f"session_id leaked into data JSON: {data_json!r}"


def test_supports_iff_supported_event_types_for_p1_floor() -> None:
    """For every value in supported_event_types(), supports('claude_code', et)
    is True. Mirrors DT-8 but scoped to the P1 floor explicitly."""
    adapter = ClaudeCodeAdapter()
    for et in P1_FLOOR_EVENT_TYPES:
        assert adapter.supports(_AGENT_NAME, et) is True, (
            f"P1 floor event_type {et!r} not honoured by supports()"
        )


def test_user_prompt_submit_data_shape() -> None:
    """Per-event happy path: user_prompt fixture produces action_metadata
    with prompt_length, transcript_path, cwd — and nothing else."""
    fixture = _load_fixture(FIXTURE_DIR / "user_prompt_submit.json")
    envelope = _envelope_from_fixture(fixture)
    adapter = ClaudeCodeAdapter()
    partial = adapter.normalize(envelope, EventType.USER_PROMPT.value)
    assert partial.event_type == EventType.USER_PROMPT
    assert "action_metadata" in partial.data
    metadata = partial.data["action_metadata"]
    assert metadata["prompt_length"] == len(PRIVACY_CANARY_VALUE)
    assert "transcript_path" in metadata
    assert "cwd" in metadata


def test_unsupported_event_type_raises_value_error() -> None:
    """Calling normalize() with an out-of-floor event_type is loud, not
    silent. THINKING is a real EventType but plan §8 defers it from P1."""
    envelope = HookEnvelope(
        project_id="test-proj",
        session_id="sess-1",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"hook_event_name": "Stop"},
    )
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ValueError):
        adapter.normalize(envelope, EventType.THINKING.value)


def test_hook_event_name_mismatch_raises() -> None:
    """If hook_event_name in payload contradicts the dispatched event_type,
    the adapter raises rather than normalising across event types."""
    envelope = HookEnvelope(
        project_id="test-proj",
        session_id="sess-1",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={
            "hook_event_name": "SessionEnd",
            "tool_name": "Read",
        },
    )
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.normalize(envelope, EventType.TOOL_USE_START.value)
    msg = str(exc_info.value)
    # Either side of the mismatch should be named so the operator can debug.
    assert "SessionEnd" in msg or "PreToolUse" in msg, (
        f"Mismatch error must name the offending hook names — was {msg!r}"
    )


def test_drop_list_is_frozenset_and_nonempty() -> None:
    """DROP_LIST is the single declarative source for privacy enforcement.
    Asserting its shape catches an accidental rename to a mutable set or an
    empty constant (which would silently disable DT-6)."""
    assert isinstance(DROP_LIST, frozenset)
    assert len(DROP_LIST) >= 5, (
        f"DROP_LIST appears too small ({len(DROP_LIST)} entries) — plan §5 "
        f"lists at least: command, content, old_string, new_string, output, "
        f"error, prompt"
    )
