"""Obsolete SessionStart injection route guards.

The agent-visible injection contract now lives at
``/hook/injection/session-start/{agent}``.  These tests keep the old
``/hook/session-start`` plain-text convention envelope from remaining a
parallel passing contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from secondsight.api.server import create_app


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def test_dt_legacy_session_start_injection_route_no_longer_returns_plain_text_envelope(
    tmp_path: Path,
) -> None:
    """DC1: plain-text ``conventions`` is not proof of valid injection."""
    home = tmp_path / ".secondsight"
    home.mkdir()

    with _client(home) as client:
        response = client.post(
            "/hook/session-start",
            json={"project_id": "proj-1", "agent": "claude_code"},
        )

    assert response.status_code != 200
    if response.content:
        assert "conventions" not in response.text
        assert "budget_total" not in response.text


def test_session_start_observation_ingest_route_remains_available(
    tmp_path: Path,
) -> None:
    """Removing old injection must not remove SessionStart observation ingest."""
    home = tmp_path / ".secondsight"
    home.mkdir()

    with _client(home) as client:
        response = client.post(
            "/hook/claude_code/session_start",
            json={
                "event_id": "evt-session-start-1",
                "timestamp": datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
                "sequence_number": 0,
                "payload": {
                    "session_id": "sess-1",
                    "cwd": str(tmp_path / "proj-1"),
                    "transcript_path": str(tmp_path / "transcript.jsonl"),
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                },
            },
        )

    assert response.status_code == 200, response.text
    assert json.loads(response.text) == {"status": "ok"}
