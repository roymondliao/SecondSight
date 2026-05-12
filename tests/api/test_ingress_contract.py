"""Thin ingress contract tests for POST /hook/{agent}/{event_type}."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient


def _make_app(tmp_secondsight_home: Path):  # type: ignore[return]
    from secondsight.api.server import create_app

    return create_app(secondsight_home=tmp_secondsight_home)


def test_thin_ingress_accepts_raw_claude_payload(tmp_secondsight_home: Path) -> None:
    app = _make_app(tmp_secondsight_home)
    body = {
        "event_id": "evt-ingress-001",
        "timestamp": datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc).isoformat(),
        "sequence_number": 0,
        "payload": {
            "session_id": "sess-ingress-001",
            "cwd": "/tmp/proj-ingress",
            "transcript_path": "/tmp/transcript.jsonl",
            "hook_event_name": "SessionStart",
            "source": "startup",
        },
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/claude_code/session_start", json=body)
        assert response.status_code == 200, response.text
        time.sleep(0.2)

    project_dir = tmp_secondsight_home / "projects" / "proj-ingress"
    db_path = project_dir / "intelligence.db"
    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT id, session_id, sequence_number, event_type FROM events WHERE id = ?",
            ("evt-ingress-001",),
        ).fetchone()
    assert row == ("evt-ingress-001", "sess-ingress-001", 0, "session_start")

    ingress_files = list(project_dir.glob("sessions/sess-ingress-001/ingress/*.json"))
    assert len(ingress_files) == 1, ingress_files
    event_files = list(project_dir.glob("sessions/sess-ingress-001/events/*.json"))
    assert len(event_files) == 1, event_files


def test_thin_ingress_missing_sequence_number_returns_422(
    tmp_secondsight_home: Path,
) -> None:
    app = _make_app(tmp_secondsight_home)
    body = {
        "event_id": "evt-ingress-002",
        "timestamp": datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc).isoformat(),
        "payload": {
            "session_id": "sess-ingress-002",
            "cwd": "/tmp/proj-ingress",
            "hook_event_name": "SessionStart",
        },
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/claude_code/session_start", json=body)
    assert response.status_code == 422


def test_thin_ingress_missing_raw_session_id_is_adapter_error(
    tmp_secondsight_home: Path,
) -> None:
    app = _make_app(tmp_secondsight_home)
    body = {
        "event_id": "evt-ingress-003",
        "timestamp": datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc).isoformat(),
        "sequence_number": 0,
        "payload": {
            "cwd": "/tmp/proj-ingress",
            "hook_event_name": "SessionStart",
        },
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/claude_code/session_start", json=body)
    assert response.status_code == 422
    assert "session_id" in response.text
