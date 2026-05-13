"""Fixture-validity tests for tests/fixtures/codex."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

from secondsight.event import EventType


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
README_PATH = FIXTURE_DIR / "_README.md"

P1_HOOK_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
    }
)

VALID_SOURCES: frozenset[str] = frozenset({"verified", "documented"})
PRIVACY_CANARY_VALUE = "PRIVACY_CANARY_DO_NOT_STORE"
CAPTURE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _resolve_path(payload: Any, dotted_path: str) -> Any:
    cursor: Any = payload
    for segment in dotted_path.split("."):
        if not isinstance(cursor, dict):
            raise KeyError(f"path {dotted_path!r}: segment before {segment!r} is not a dict")
        if segment not in cursor:
            raise KeyError(f"path {dotted_path!r}: missing key {segment!r}")
        cursor = cursor[segment]
    return cursor


def _value_appears_in(haystack: Any, needle: str) -> bool:
    if isinstance(haystack, str):
        return needle in haystack
    if isinstance(haystack, dict):
        return any(_value_appears_in(v, needle) for v in haystack.values())
    if isinstance(haystack, list):
        return any(_value_appears_in(v, needle) for v in haystack)
    return False


def test_fixture_set_is_complete() -> None:
    names = {
        json.loads(path.read_text(encoding="utf-8"))["_meta"]["_codex_hook_event_name"]
        for path in _fixture_paths()
    }
    assert names == P1_HOOK_EVENT_NAMES


def test_fixture_readme_documents_verified_contract() -> None:
    assert README_PATH.exists(), f"missing fixture contract README: {README_PATH}"
    body = README_PATH.read_text(encoding="utf-8")
    for expected in (
        "2026-05-13",
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "prompt_text",
        "tool_response",
        "last_assistant_message",
    ):
        assert expected in body, f"fixture README must mention {expected!r}"


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_meta_source_is_tracked(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_meta"]["_source"] in VALID_SOURCES
    if data["_meta"]["_source"] == "verified":
        assert data["_meta"].get("_capture_origin"), (
            f"{path.name}: verified fixture must record _capture_origin"
        )


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_verified_fixtures_have_structured_capture_metadata(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data["_meta"]
    if meta["_source"] != "verified":
        pytest.skip("documented fixture")

    capture_date = meta.get("_capture_date")
    capture_file = meta.get("_capture_file")
    substituted = meta.get("_raw_fields_substituted")

    assert isinstance(capture_date, str) and CAPTURE_DATE_RE.fullmatch(capture_date), (
        f"{path.name}: verified fixture must carry _capture_date in YYYY-MM-DD form"
    )
    assert isinstance(capture_file, str) and capture_file.endswith(".json"), (
        f"{path.name}: verified fixture must carry _capture_file"
    )
    assert meta["_codex_hook_event_name"] in capture_file, (
        f"{path.name}: capture file should mention hook event name, got {capture_file!r}"
    )
    assert isinstance(substituted, list), (
        f"{path.name}: verified fixture must declare _raw_fields_substituted"
    )
    assert all(isinstance(item, str) and item for item in substituted), (
        f"{path.name}: _raw_fields_substituted must be a list[str]"
    )


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_secondsight_event_type_is_real(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    valid = {member.value for member in EventType}
    assert data["_meta"]["_secondsight_event_type"] in valid


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_codex_hook_event_name_is_in_floor(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_meta"]["_codex_hook_event_name"] in P1_HOOK_EVENT_NAMES


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_privacy_canary_is_placed_and_not_preleaked(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    payload = data["payload"]
    expected = data["expected_partial_event_data"]
    canary = data["privacy_canary"]
    canary_field = data["_meta"]["_privacy_canary_field"]

    assert canary == PRIVACY_CANARY_VALUE
    assert canary in str(_resolve_path(payload, canary_field))
    if path.name != "user_prompt_submit.json":
        assert not _value_appears_in(expected, canary)
