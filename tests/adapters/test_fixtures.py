"""Fixture-validity death tests (task-2, P1-9-fixtures).

These tests guard the empirical contract that ClaudeCodeAdapter (task-4) and
the integration test (task-5) consume. There is NO production code in task-2;
the fixtures themselves are the deliverable, so the death tests assert
fixture-level invariants:

    DT-1  fixture parses as JSON
    DT-2  _meta._source ∈ {"verified", "documented"}
    DT-3  privacy_canary value appears at the path declared in
          _meta._privacy_canary_field within `payload`, AND does NOT
          appear anywhere in `expected_partial_event_data`
    DT-4  _meta._secondsight_event_type is a real EventType enum value
    DT-5  _meta._claude_code_hook_event_name ∈ Phase-1-floor hook event set

If any of these go red after task-2 lands, a fixture has drifted from the
contract that task-4 / task-5 were built against, and the regression must
fail loudly rather than silently mis-train the adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from secondsight.event import EventType


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code"

# P1 floor — see plan §7 G1 mapping table. Other Claude Code hooks
# (Stop, SubagentStop, Notification, PreCompact) are out of P1 scope.
P1_HOOK_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "SessionStart",
        "SessionEnd",
    }
)

VALID_SOURCES: frozenset[str] = frozenset({"verified", "documented"})

PRIVACY_CANARY_VALUE = "PRIVACY_CANARY_DO_NOT_STORE"


def _fixture_paths() -> list[Path]:
    """Discover all .json fixtures under tests/fixtures/claude_code/."""
    return sorted(FIXTURE_DIR.glob("*.json"))


def _resolve_path(payload: Any, dotted_path: str) -> Any:
    """Resolve a simple dotted path (e.g. 'tool_input.command') against a dict.

    Raises KeyError if any intermediate key is missing — this is the desired
    behavior for DT-3, since a fixture that declares a canary path it does not
    actually populate is broken.
    """
    cursor: Any = payload
    for segment in dotted_path.split("."):
        if not isinstance(cursor, dict):
            raise KeyError(f"path {dotted_path!r}: segment before {segment!r} is not a dict")
        if segment not in cursor:
            raise KeyError(f"path {dotted_path!r}: missing key {segment!r}")
        cursor = cursor[segment]
    return cursor


def _value_appears_in(haystack: Any, needle: str) -> bool:
    """Recursively check whether `needle` appears as a string value or
    substring within any string value in `haystack` (which may be dict/list/scalar)."""
    if isinstance(haystack, str):
        return needle in haystack
    if isinstance(haystack, dict):
        return any(_value_appears_in(v, needle) for v in haystack.values())
    if isinstance(haystack, list):
        return any(_value_appears_in(v, needle) for v in haystack)
    return False


@pytest.fixture(scope="session")
def fixture_paths() -> list[Path]:
    paths = _fixture_paths()
    if not paths:
        pytest.fail(f"no fixtures found under {FIXTURE_DIR} — task-2 deliverable missing")
    return paths


# ---------------------------------------------------------------------------
# Death tests (DT-1..DT-5)
# ---------------------------------------------------------------------------


def test_dt1_every_fixture_parses_as_json(fixture_paths: list[Path]) -> None:
    """DT-1: every fixture is valid JSON. A malformed fixture would crash
    task-4 / task-5 at collection time with a JSONDecodeError that obscures
    the real problem (which is fixture authorship, not adapter logic)."""
    for path in fixture_paths:
        with path.open("r", encoding="utf-8") as fh:
            try:
                json.load(fh)
            except json.JSONDecodeError as exc:
                pytest.fail(f"{path.name}: invalid JSON — {exc}")


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt2_meta_source_is_verified_or_documented(path: Path) -> None:
    """DT-2: _source must be a tracked provenance label. Any other value
    means the fixture was added without explicit provenance, which is the
    "invented payload" failure the autopsy named as a kill condition."""
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("_meta", {})
    source = meta.get("_source")
    assert source in VALID_SOURCES, (
        f"{path.name}: _meta._source={source!r} not in {sorted(VALID_SOURCES)}"
    )


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt3_privacy_canary_present_and_placed(path: Path) -> None:
    """DT-3: every fixture must (a) declare which payload field holds the
    canary via _meta._privacy_canary_field, (b) actually contain the canary
    string at that path inside `payload`, and (c) NOT contain the canary
    anywhere in `expected_partial_event_data`.

    The canary's only job is to fail the adapter test when a value that
    should have been dropped slips through. If expected_partial_event_data
    contains the canary, the test would pass on regression — defeating the
    purpose. So this is a fixture-authorship invariant."""
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("_meta", {})
    payload = data.get("payload", {})
    expected = data.get("expected_partial_event_data", {})

    canary = data.get("privacy_canary")
    assert canary == PRIVACY_CANARY_VALUE, (
        f"{path.name}: privacy_canary must equal {PRIVACY_CANARY_VALUE!r}, got {canary!r}"
    )

    canary_field = meta.get("_privacy_canary_field")
    assert isinstance(canary_field, str) and canary_field, (
        f"{path.name}: _meta._privacy_canary_field must be a non-empty path string"
    )

    try:
        value_at_path = _resolve_path(payload, canary_field)
    except KeyError as exc:
        pytest.fail(f"{path.name}: canary path resolution failed — {exc}")

    assert canary in str(value_at_path), (
        f"{path.name}: canary value {canary!r} not found at "
        f"payload.{canary_field}={value_at_path!r}"
    )

    assert not _value_appears_in(expected, canary), (
        f"{path.name}: canary leaked into expected_partial_event_data — "
        f"the regression test is broken before adapter is even written"
    )


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt4_secondsight_event_type_is_real(path: Path) -> None:
    """DT-4: _meta._secondsight_event_type must be a real EventType enum
    value. Catches typos and stale fixtures referencing event types that
    were renamed in event.py."""
    data = json.loads(path.read_text(encoding="utf-8"))
    et = data.get("_meta", {}).get("_secondsight_event_type")
    valid = {member.value for member in EventType}
    assert et in valid, f"{path.name}: _meta._secondsight_event_type={et!r} not in {sorted(valid)}"


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_dt5_claude_code_hook_event_name_in_p1_floor(path: Path) -> None:
    """DT-5: _meta._claude_code_hook_event_name must be one of the P1-floor
    hook events. Stop / SubagentStop / Notification / PreCompact are
    deliberately excluded from P1 (plan §7 G1) and adding them via fixture
    drift would silently expand scope past the death-condition gate."""
    data = json.loads(path.read_text(encoding="utf-8"))
    name = data.get("_meta", {}).get("_claude_code_hook_event_name")
    assert name in P1_HOOK_EVENT_NAMES, (
        f"{path.name}: _meta._claude_code_hook_event_name={name!r} "
        f"not in P1 floor {sorted(P1_HOOK_EVENT_NAMES)}"
    )


# ---------------------------------------------------------------------------
# Coverage guard — protect against silent under-coverage of the P1 floor
# ---------------------------------------------------------------------------


def test_p1_floor_fully_covered(fixture_paths: list[Path]) -> None:
    """All five P1-floor hook event names must be represented by exactly one
    fixture. A regression that deletes a fixture or merges two would silently
    shrink the contract surface task-4 is verified against."""
    seen: set[str] = set()
    for path in fixture_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data.get("_meta", {}).get("_claude_code_hook_event_name")
        if name in seen:
            pytest.fail(
                f"{path.name}: duplicate _claude_code_hook_event_name={name!r} — "
                "P1 floor expects exactly one fixture per event"
            )
        seen.add(name)
    missing = P1_HOOK_EVENT_NAMES - seen
    assert not missing, f"P1-floor fixtures missing for: {sorted(missing)}"
