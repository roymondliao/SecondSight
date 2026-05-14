"""Codex thin-ingress contract tests using verified hook fixtures."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from secondsight.api.ingress import project_id_from_cwd


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _make_app(tmp_secondsight_home: Path):  # type: ignore[return]
    from secondsight.api.server import create_app

    return create_app(secondsight_home=tmp_secondsight_home)


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _request_body(
    fixture: dict[str, Any], *, event_id: str, sequence_number: int
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "timestamp": datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc).isoformat(),
        "sequence_number": sequence_number,
        "payload": fixture["payload"],
    }


def _project_dir(tmp_secondsight_home: Path, fixture: dict[str, Any]) -> Path:
    project_id = project_id_from_cwd(fixture["payload"]["cwd"])
    return tmp_secondsight_home / "projects" / project_id


def _db_row(project_dir: Path, event_id: str) -> tuple[str, str] | None:
    db_path = project_dir / "intelligence.db"
    if not db_path.exists():
        return None
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT event_type, data FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _event_file_data(
    project_dir: Path,
    session_id: str,
    *,
    event_type: str,
    sequence_number: int,
) -> dict[str, Any]:
    pattern = f"*_{event_type}_seq{sequence_number:06d}.json"
    event_files = sorted((project_dir / "sessions" / session_id / "events").glob(pattern))
    assert len(event_files) == 1, event_files
    return json.loads(event_files[0].read_text(encoding="utf-8"))["data"]


def test_dt_codex_user_prompt_persists_exact_prompt_text(
    tmp_secondsight_home: Path,
) -> None:
    fixture = _load_fixture("user_prompt_submit.json")
    body = _request_body(fixture, event_id="evt-codex-user-prompt-001", sequence_number=0)
    project_dir = _project_dir(tmp_secondsight_home, fixture)

    app = _make_app(tmp_secondsight_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/codex/user_prompt", json=body)
        assert response.status_code == 200, response.text
        time.sleep(0.3)

    row = _db_row(project_dir, body["event_id"])
    assert row is not None
    assert row[0] == "user_prompt"
    persisted = json.loads(row[1])
    assert persisted["action_metadata"]["prompt_text"] == fixture["payload"]["prompt"]
    assert "prompt_length" not in persisted["action_metadata"]
    assert (
        _event_file_data(
            project_dir,
            fixture["payload"]["session_id"],
            event_type="user_prompt",
            sequence_number=0,
        )
        == persisted
    )


def test_dt_codex_post_tool_and_stop_drop_raw_fields_in_persisted_event_data(
    tmp_secondsight_home: Path,
) -> None:
    app = _make_app(tmp_secondsight_home)

    cases = [
        (
            "post_tool_use.json",
            "evt-codex-post-tool-001",
            0,
            "tool_use_end",
            "tool_response",
            "call_wsvNx1yIEZczeuKwEfEe2Tat",
        ),
        (
            "stop.json",
            "evt-codex-stop-001",
            1,
            "session_end",
            "last_assistant_message",
            None,
        ),
    ]

    with TestClient(app, raise_server_exceptions=False) as client:
        for fixture_name, event_id, sequence_number, route_event_type, _, _ in cases:
            fixture = _load_fixture(fixture_name)
            response = client.post(
                f"/hook/codex/{route_event_type}",
                json=_request_body(
                    fixture,
                    event_id=event_id,
                    sequence_number=sequence_number,
                ),
            )
            assert response.status_code == 200, response.text
        time.sleep(0.3)

    for (
        fixture_name,
        event_id,
        sequence_number,
        expected_event_type,
        forbidden_key,
        expected_tool_use_id,
    ) in cases:
        fixture = _load_fixture(fixture_name)
        project_dir = _project_dir(tmp_secondsight_home, fixture)
        row = _db_row(project_dir, event_id)
        assert row is not None
        assert row[0] == expected_event_type

        persisted = json.loads(row[1])
        persisted_json = json.dumps(persisted, ensure_ascii=False)
        assert forbidden_key not in persisted
        assert fixture["privacy_canary"] not in persisted_json
        assert (
            _event_file_data(
                project_dir,
                fixture["payload"]["session_id"],
                event_type=expected_event_type,
                sequence_number=sequence_number,
            )
            == persisted
        )
        if expected_tool_use_id is not None:
            assert persisted["tool_use_id"] == expected_tool_use_id


def test_dt_codex_route_payload_mismatch_returns_422(tmp_secondsight_home: Path) -> None:
    fixture = _load_fixture("user_prompt_submit.json")
    body = _request_body(fixture, event_id="evt-codex-mismatch-001", sequence_number=0)
    project_dir = _project_dir(tmp_secondsight_home, fixture)

    app = _make_app(tmp_secondsight_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/codex/tool_use_end", json=body)

    assert response.status_code == 422
    assert "Route/payload mismatch" in response.text
    assert not project_dir.exists(), "mismatched payload must not materialize project storage"
