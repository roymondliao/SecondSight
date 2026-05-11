"""Death tests + unit tests for CodexAdapter (P3B-8, GUR-109).

Death tests verify the silent-failure surface:
    DT-1  supports("nonexistent", *)           -> False
    DT-2  supports("codex", "<unknown>")        -> False
    DT-3  privacy canary absent from data per fixture
    DT-4  envelope missing session_id           -> ValueError
    DT-5  payload missing hook_event_name       -> ValueError
    DT-6  DROP_LIST raw values absent from data per fixture
    DT-7  inject_hint returns empty string
    DT-8  supports() ↔ supported_event_types() consistent
    DT-9  _DATA_BUILDERS keys match _HOOK_TO_EVENT_TYPE values
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from secondsight.adapters.codex import (
    DROP_LIST,
    CodexAdapter,
)
from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType


_AGENT_NAME = "codex"

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
PRIVACY_CANARY_VALUE = "PRIVACY_CANARY_DO_NOT_STORE"


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _load_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _envelope_from_fixture(fixture: Mapping[str, Any]) -> HookEnvelope:
    payload = dict(fixture["payload"])
    return HookEnvelope(
        project_id="test-proj",
        session_id=str(payload.get("session_id", "test-sess")),
        agent=_AGENT_NAME,
        event_id="test-evt-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload=payload,
    )


def _resolve_path(payload: Any, dotted_path: str) -> Any:
    cursor: Any = payload
    for segment in dotted_path.split("."):
        if not isinstance(cursor, Mapping):
            return None
        if segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


# ---------------------------------------------------------------------------
# DEATH TESTS
# ---------------------------------------------------------------------------


def test_dt1_unknown_agent_returns_false() -> None:
    adapter = CodexAdapter()
    assert adapter.supports("nonexistent", EventType.TOOL_USE_END.value) is False
    assert adapter.supports("claude_code", EventType.TOOL_USE_END.value) is False
    assert adapter.supports("Codex", EventType.TOOL_USE_END.value) is False
    assert adapter.supports("", EventType.TOOL_USE_END.value) is False


def test_dt2_unknown_event_type_returns_false() -> None:
    adapter = CodexAdapter()
    assert adapter.supports(_AGENT_NAME, "blarg") is False
    assert adapter.supports(_AGENT_NAME, "") is False
    assert adapter.supports(_AGENT_NAME, EventType.THINKING.value) is False
    assert adapter.supports(_AGENT_NAME, EventType.TOOL_USE_START.value) is False


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt3_privacy_canary_absent_from_data(path: Path) -> None:
    fixture = _load_fixture(path)
    if "privacy_canary" not in fixture:
        pytest.skip("no privacy canary in fixture")
    event_type = fixture["_meta"]["_secondsight_event_type"]
    envelope = _envelope_from_fixture(fixture)

    adapter = CodexAdapter()
    partial = adapter.normalize(envelope, event_type)

    data_json = json.dumps(partial.data, default=str)
    assert PRIVACY_CANARY_VALUE not in data_json, (
        f"{path.name}: canary value leaked into PartialEvent.data — data JSON: {data_json!r}"
    )


def test_dt4_envelope_missing_session_id_raises() -> None:
    envelope = HookEnvelope.model_construct(
        project_id="test-proj",
        session_id="",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"hook_event_name": "session_start"},
    )
    adapter = CodexAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.normalize(envelope, EventType.SESSION_START.value)
    assert "session_id" in str(exc_info.value)


def test_dt5_payload_missing_hook_event_name_raises() -> None:
    envelope = HookEnvelope(
        project_id="test-proj",
        session_id="sess-1",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"cwd": "/tmp"},
    )
    adapter = CodexAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.normalize(envelope, EventType.SESSION_START.value)
    assert "hook_event_name" in str(exc_info.value)


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt6_drop_list_raw_values_absent_from_data(path: Path) -> None:
    fixture = _load_fixture(path)
    event_type = fixture["_meta"]["_secondsight_event_type"]
    envelope = _envelope_from_fixture(fixture)

    adapter = CodexAdapter()
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


def test_dt7_inject_hint_returns_empty_string() -> None:
    adapter = CodexAdapter()
    result = adapter.inject_hint(object())  # type: ignore[arg-type]
    assert result == ""


def test_dt8_supports_consistency_with_supported_event_types() -> None:
    adapter = CodexAdapter()
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
    from secondsight.adapters.codex import _DATA_BUILDERS, _HOOK_TO_EVENT_TYPE

    assert set(_HOOK_TO_EVENT_TYPE.values()) == set(_DATA_BUILDERS.keys())


# ---------------------------------------------------------------------------
# UNIT TESTS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_fixture_round_trip(path: Path) -> None:
    fixture = _load_fixture(path)
    event_type = fixture["_meta"]["_secondsight_event_type"]
    expected = fixture["expected_partial_event_data"]
    envelope = _envelope_from_fixture(fixture)

    adapter = CodexAdapter()
    partial = adapter.normalize(envelope, event_type)
    assert partial.data == expected, (
        f"{path.name}: normalize().data mismatch.\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {partial.data!r}"
    )


def test_normalize_envelope_fields_forwarded() -> None:
    fixture = _load_fixture(FIXTURE_DIR / "session_start.json")
    envelope = _envelope_from_fixture(fixture)
    adapter = CodexAdapter()
    partial = adapter.normalize(envelope, EventType.SESSION_START.value)
    assert partial.id == envelope.event_id
    assert partial.session_id == envelope.session_id
    assert partial.project_id == envelope.project_id
    assert partial.event_type == EventType.SESSION_START
    assert partial.timestamp == envelope.timestamp
    assert partial.sequence_number == envelope.sequence_number


def test_post_tool_use_duration_ms_forwarded() -> None:
    fixture = _load_fixture(FIXTURE_DIR / "post_tool_use.json")
    envelope = _envelope_from_fixture(fixture)
    adapter = CodexAdapter()
    partial = adapter.normalize(envelope, EventType.TOOL_USE_END.value)
    assert partial.duration_ms == 42


def test_unsupported_event_type_raises_value_error() -> None:
    envelope = HookEnvelope(
        project_id="test-proj",
        session_id="sess-1",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"hook_event_name": "session_start"},
    )
    adapter = CodexAdapter()
    with pytest.raises(ValueError):
        adapter.normalize(envelope, EventType.THINKING.value)


def test_hook_event_name_mismatch_raises() -> None:
    envelope = HookEnvelope(
        project_id="test-proj",
        session_id="sess-1",
        agent=_AGENT_NAME,
        event_id="evt-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        sequence_number=0,
        payload={"hook_event_name": "stop", "cwd": "/tmp"},
    )
    adapter = CodexAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.normalize(envelope, EventType.SESSION_START.value)
    msg = str(exc_info.value)
    assert "stop" in msg or "session_start" in msg


def test_inject_convention_formats_correctly() -> None:
    from secondsight.feedback.convention import Convention

    adapter = CodexAdapter()
    conv = Convention(id="c1", instruction="Read AGENTS.md first", frequency=0.9, source_flag_type="unnecessary_read")
    result = adapter.inject_convention(conv)
    assert result == "- Read AGENTS.md first"

    empty = Convention(id="c2", instruction="", frequency=0.5, source_flag_type=None)
    assert adapter.inject_convention(empty) == ""
