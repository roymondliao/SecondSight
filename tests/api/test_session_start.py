"""Death + happy-path tests for POST /hook/session-start (GUR-105, P3A-3).

Death cases:
- DT-1: Unknown agent returns 422 (no adapter found).
- DT-2: Unsafe project_id returns 422 (path traversal guard).
- DT-3: Empty project (no conventions) returns count=0, conventions="".
- DT-4: Token budget is respected — injected conventions fit ≤ 2000 tokens.
- DT-5: Disabled conventions are NOT injected (only active status).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import create_app
from secondsight.storage.directives_repository import DirectivesRepository

UTC = timezone.utc
NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def _seed_convention(
    home: Path,
    *,
    project_id: str,
    directive_id: str,
    instruction: str = "Always read AGENTS.md first.",
    frequency: float = 0.8,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
) -> None:
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    repo = DirectivesRepository(resources.db_engine)
    repo.create_schema()
    directive = Directive(
        id=directive_id,
        project_id=project_id,
        type=DirectiveType.CONVENTION,
        status=status,
        instruction=instruction,
        frequency=frequency,
        identity_key=f"key-{directive_id}",
        source_sessions=["s1"],
        source_flag_type="unnecessary_read",
        created_at=NOW,
        updated_at=NOW,
        disabled_at=NOW if status == DirectiveStatus.DISABLED else None,
        disabled_reason="test disable" if status == DirectiveStatus.DISABLED else None,
    )
    repo.insert(directive)
    asyncio.run(registry.aclose())


class TestDeathPaths:
    def test_dt_1_unknown_agent_returns_422(self, home: Path) -> None:
        """DT-1: Agent with no adapter registered → 422."""
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "proj-1", "agent": "nonexistent_agent"},
            )
        assert resp.status_code == 422

    def test_dt_2_unsafe_project_id_returns_422(self, home: Path) -> None:
        """DT-2: Path-traversal characters in project_id → 422."""
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "../etc/passwd", "agent": "claude_code"},
            )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        detail_str = str(detail).lower()
        assert "unsafe" in detail_str

    def test_dt_3_empty_project_returns_zero(self, home: Path) -> None:
        """DT-3: Project with no conventions → count=0, conventions=""."""
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "empty-proj", "agent": "claude_code"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["conventions"] == ""
        assert data["budget_used"] == 0

    def test_dt_4_token_budget_respected(self, home: Path) -> None:
        """DT-4: Many conventions → only those within budget are injected."""
        for i in range(20):
            _seed_convention(
                home,
                project_id="proj-1",
                directive_id=f"d-{i}",
                instruction="X" * 500,
                frequency=0.9 - i * 0.01,
            )
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "proj-1", "agent": "claude_code"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["budget_used"] <= data["budget_total"]
        assert data["count"] < 20

    def test_dt_5_disabled_conventions_not_injected(self, home: Path) -> None:
        """DT-5: Disabled conventions never appear in injection."""
        _seed_convention(
            home,
            project_id="proj-1",
            directive_id="active-1",
            instruction="Active rule",
            frequency=0.9,
            status=DirectiveStatus.ACTIVE,
        )
        _seed_convention(
            home,
            project_id="proj-1",
            directive_id="disabled-1",
            instruction="Disabled rule",
            frequency=0.95,
            status=DirectiveStatus.DISABLED,
        )
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "proj-1", "agent": "claude_code"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "Disabled rule" not in data["conventions"]
        assert "Active rule" in data["conventions"]


class TestHappyPath:
    def test_returns_formatted_conventions(self, home: Path) -> None:
        _seed_convention(
            home,
            project_id="proj-1",
            directive_id="d1",
            instruction="Read AGENTS.md before any implementation",
            frequency=0.9,
        )
        _seed_convention(
            home,
            project_id="proj-1",
            directive_id="d2",
            instruction="Use grep before creating new files",
            frequency=0.7,
        )
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "proj-1", "agent": "claude_code"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert "- Read AGENTS.md" in data["conventions"]
        assert "- Use grep" in data["conventions"]
        assert data["budget_total"] == 2000

    def test_response_shape(self, home: Path) -> None:
        with _client(home) as client:
            resp = client.post(
                "/hook/session-start",
                json={"project_id": "proj-1", "agent": "claude_code"},
            )
        data = resp.json()
        assert "conventions" in data
        assert "count" in data
        assert "budget_used" in data
        assert "budget_total" in data
