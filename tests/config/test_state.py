"""Tests for src/secondsight/state.py — SecondSightState lifecycle.

Death tests target silent failure paths. Samsara order: death tests first, then unit tests.

Death tests:
- DT-state-1: state.json missing → SecondSightState.load() returns None (not exception)
- DT-state-2: state.json malformed JSON → raises SecondSightStateError with path in message
- DT-state-3: state.json with init_agent = "opencode" → load() succeeds (schema valid; rejection is Task 6)
- DT-state-4: write then load round-trips all fields (schema_version, init_agent, init_at, secondsight_version)
- DT-state-5: state.json directory does not exist → save() must create it (mkdir parents=True)
- DT-state-6: SecondSightStateError must be raised with path in message when JSON malformed

Unit tests:
- UT-state-1: SecondSightState has all required fields
- UT-state-2: SecondSightState.save() creates the file
- UT-state-3: SecondSightState.load() returns None when file absent
- UT-state-4: round-trip preserves init_agent value
- UT-state-5: round-trip preserves schema_version "1.0"
- UT-state-6: round-trip preserves secondsight_version
- UT-state-7: round-trip preserves init_at as ISO format string
- UT-state-8: SecondSightStateError is an Exception subclass
- UT-state-9: save() sets schema_version = "1.0" automatically
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Death tests — must fail before implementation (ImportError or AssertionError)
# ---------------------------------------------------------------------------


class TestDTState1LoadMissingFileReturnsNone:
    """DT-state-1: state.json missing → SecondSightState.load() returns None.

    Silent failure path: if load() raises FileNotFoundError for missing state,
    every user who runs secondsight without having initialized first gets an
    unhandled exception, not a clean "no state" signal for the caller to handle.
    """

    def test_load_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        absent_path = tmp_path / "nonexistent" / "state.json"
        result = SecondSightState.load(absent_path)

        assert result is None, (
            f"SecondSightState.load() must return None for missing file. "
            f"Got {result!r}. If it raises, callers must wrap in try/except — "
            "but the contract is None-on-missing so callers can use `if state is None`."
        )

    def test_load_returns_none_for_absent_directory(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        result = SecondSightState.load(tmp_path / "no" / "dir" / "state.json")
        assert result is None


class TestDTState2MalformedJsonRaisesStateError:
    """DT-state-2: state.json malformed JSON → raises SecondSightStateError with path in message.

    Silent failure path: if malformed JSON silently returns None, the user never
    knows their state file is corrupt. First symptom: wrong agent used for analysis.
    The path in the error message is critical for the user to find and fix the file.
    """

    def test_malformed_json_raises_state_error(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState, SecondSightStateError

        state_path = tmp_path / "state.json"
        state_path.write_text("{ this is not valid json }", encoding="utf-8")

        with pytest.raises(SecondSightStateError) as exc_info:
            SecondSightState.load(state_path)

        assert str(state_path) in str(exc_info.value), (
            f"Error message must contain the path {state_path!r} so users know "
            f"which file to fix. Got: {exc_info.value!r}"
        )

    def test_malformed_json_error_is_state_error_not_json_error(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState, SecondSightStateError

        state_path = tmp_path / "state.json"
        state_path.write_text("not json at all", encoding="utf-8")

        with pytest.raises(SecondSightStateError):
            SecondSightState.load(state_path)
        # Ensure it's NOT a bare json.JSONDecodeError propagating
        # (SecondSightStateError must be the outward-facing exception)


class TestDTState3OpenCodeAgentSchemaValid:
    """DT-state-3: state.json with init_agent = "opencode" → load() succeeds.

    Schema validation does NOT reject "opencode" at state.py level.
    Rejection of unsupported agents happens in Task 6 (dispatch pre-check).
    This test confirms state.py is not overreaching its responsibility.
    """

    def test_opencode_agent_loads_successfully(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "init_agent": "opencode",
                    "init_at": "2026-05-14T13:42:18+08:00",
                    "secondsight_version": "0.1.0",
                }
            ),
            encoding="utf-8",
        )

        state = SecondSightState.load(state_path)
        assert state is not None, "load() must succeed for valid JSON with opencode agent"
        assert state.init_agent == "opencode"


class TestDTState4RoundTripAllFields:
    """DT-state-4: write then load round-trips all fields.

    This tests the schema contract: if any field is lost in the round-trip,
    callers silently get stale or missing data (e.g., wrong secondsight_version
    causes incorrect upgrade detection).
    """

    def test_round_trip_all_fields(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        state_path = tmp_path / "state.json"
        original = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T13:42:18+08:00",
            secondsight_version="0.1.0-test",
        )
        original.save(state_path)

        loaded = SecondSightState.load(state_path)
        assert loaded is not None

        assert loaded.schema_version == "1.0"
        assert loaded.init_agent == "claude_code"
        assert loaded.init_at == "2026-05-14T13:42:18+08:00"
        assert loaded.secondsight_version == "0.1.0-test"


class TestDTState5SaveCreatesDirectory:
    """DT-state-5: state.json directory does not exist → save() must create it.

    DC: ~/.secondsight/ may not exist on first `secondsight init` run.
    Silent failure path: if save() raises FileNotFoundError instead of creating
    the directory, init silently fails to persist agent selection, and every
    subsequent 'auto' resolution falls through to the wrong default.
    """

    def test_save_creates_missing_directory(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        # Deep path that does not exist
        state_path = tmp_path / "new_dir" / "nested" / "state.json"
        assert not state_path.parent.exists()

        state = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T13:42:18+08:00",
            secondsight_version="0.1.0",
        )
        # Must NOT raise
        state.save(state_path)

        assert state_path.exists(), "state.json must be created at the specified path"


class TestDTState6ErrorWithPath:
    """DT-state-6: SecondSightStateError message must contain the path for diagnostics."""

    def test_error_message_includes_path(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState, SecondSightStateError

        state_path = tmp_path / "bad_state.json"
        state_path.write_text("{invalid}", encoding="utf-8")

        with pytest.raises(SecondSightStateError) as exc_info:
            SecondSightState.load(state_path)

        error_str = str(exc_info.value)
        # The path should appear in the error so users can find the file
        assert "bad_state.json" in error_str or str(tmp_path) in error_str, (
            f"Error message must include the state file path. Got: {error_str!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests (happy path)
# ---------------------------------------------------------------------------


class TestUTState1DataclassFields:
    """UT-state-1: SecondSightState has all required fields."""

    def test_required_fields_exist(self) -> None:
        from dataclasses import fields

        from secondsight.state import SecondSightState

        field_names = {f.name for f in fields(SecondSightState)}
        assert "schema_version" in field_names
        assert "init_agent" in field_names
        assert "init_at" in field_names
        assert "secondsight_version" in field_names


class TestUTState2SaveCreatesFile:
    """UT-state-2: SecondSightState.save() creates the JSON file."""

    def test_save_creates_file(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        state_path = tmp_path / "state.json"
        state = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T00:00:00+00:00",
            secondsight_version="0.1.0",
        )
        state.save(state_path)

        assert state_path.exists()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["init_agent"] == "claude_code"


class TestUTState3LoadReturnsNone:
    """UT-state-3: SecondSightState.load() returns None for absent file."""

    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        result = SecondSightState.load(tmp_path / "nonexistent.json")
        assert result is None


class TestUTState4RoundTripInitAgent:
    """UT-state-4: round-trip preserves init_agent."""

    def test_codex_agent_roundtrip(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        path = tmp_path / "state.json"
        s = SecondSightState(
            schema_version="1.0",
            init_agent="codex",
            init_at="2026-05-14T00:00:00+00:00",
            secondsight_version="0.0.1",
        )
        s.save(path)
        loaded = SecondSightState.load(path)
        assert loaded is not None
        assert loaded.init_agent == "codex"


class TestUTState5SchemaVersion:
    """UT-state-5: round-trip preserves schema_version "1.0"."""

    def test_schema_version_preserved(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        path = tmp_path / "state.json"
        s = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T00:00:00+00:00",
            secondsight_version="0.0.1",
        )
        s.save(path)
        loaded = SecondSightState.load(path)
        assert loaded is not None
        assert loaded.schema_version == "1.0"


class TestUTState6SecondsightVersionPreserved:
    """UT-state-6: round-trip preserves secondsight_version."""

    def test_version_preserved(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        path = tmp_path / "state.json"
        s = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T00:00:00+00:00",
            secondsight_version="1.2.3",
        )
        s.save(path)
        loaded = SecondSightState.load(path)
        assert loaded is not None
        assert loaded.secondsight_version == "1.2.3"


class TestUTState7InitAtPreserved:
    """UT-state-7: round-trip preserves init_at as string."""

    def test_init_at_preserved(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        path = tmp_path / "state.json"
        ts = "2026-05-14T13:42:18+08:00"
        s = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at=ts,
            secondsight_version="0.1.0",
        )
        s.save(path)
        loaded = SecondSightState.load(path)
        assert loaded is not None
        assert loaded.init_at == ts


class TestUTState8StateErrorIsException:
    """UT-state-8: SecondSightStateError is an Exception subclass."""

    def test_error_is_exception(self) -> None:
        from secondsight.state import SecondSightStateError

        assert issubclass(SecondSightStateError, Exception)


class TestUTState9SavedJsonHasSchemaVersion:
    """UT-state-9: save() writes schema_version = "1.0" to JSON."""

    def test_json_has_schema_version(self, tmp_path: Path) -> None:
        from secondsight.state import SecondSightState

        path = tmp_path / "state.json"
        s = SecondSightState(
            schema_version="1.0",
            init_agent="claude_code",
            init_at="2026-05-14T00:00:00+00:00",
            secondsight_version="0.1.0",
        )
        s.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("schema_version") == "1.0"
