"""Tests for the shared id-safety helper (GUR-147 review hardening).

Pins the three call-site validators added in response to security review:
- HIGH-1: Observation API rejects unsafe project_id with 422.
- MEDIUM-1: cleanup CLI rejects unsafe --project-id with exit 2.
- MEDIUM-2: retention purger raises ValueError for unsafe session_id
  before shutil.rmtree (covered in tests/storage/test_retention_purger.py).

Plus low-level coverage of `is_safe_id` itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secondsight.api._id_safety import is_safe_id
from secondsight.api.server import create_app


# ---------------------------------------------------------------------------
# Pure helper coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "p1",
        "com.company.project",
        "alpha-beta_gamma:v1",
        "0123456789",
    ],
)
def test_is_safe_id_accepts_normal(value: str) -> None:
    assert is_safe_id(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "....",
        "../etc",
        "..\\windows",
        "/abs/path",
        "x/y",
        "x\\y",
        "tab\there",
        "newline\nhere",
        "null\x00byte",
        "control\x01chr",
        "del\x7fchar",
    ],
)
def test_is_safe_id_rejects_unsafe(value: str) -> None:
    assert is_safe_id(value) is False


# ---------------------------------------------------------------------------
# Observation API HIGH-1 fix — project_id traversal characters → 422.
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _client(home: Path) -> TestClient:
    return TestClient(create_app(secondsight_home=home), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "bad_pid",
    [
        "../../tmp/pwn",
        "..",
        "x/y",
        "x\\y",
        "null\x00byte",
        ".",
    ],
)
def test_observation_rejects_unsafe_project_id_422(home: Path, bad_pid: str) -> None:
    with _client(home) as client:
        r = client.get("/api/sessions", params={"project_id": bad_pid})
    assert r.status_code == 422, r.text
    assert "unsafe characters" in r.text or "project_id" in r.text


def test_observation_rejects_unsafe_project_id_does_not_create_dir(
    home: Path,
) -> None:
    """Ensure a malicious request does NOT cause _build_resources to mkdir
    outside the SecondSight home root.
    """
    with _client(home) as client:
        client.get("/api/sessions", params={"project_id": "../../escape"})

    # No directory named "escape" should exist outside home.
    escape = home.parent.parent / "escape"
    assert not escape.exists(), f"Path traversal created {escape}"
    # And no malformed entry under home/projects either.
    for child in (home / "projects").glob("*") if (home / "projects").exists() else []:
        assert ".." not in child.name
        assert "/" not in child.name


def test_observation_session_endpoints_also_reject_unsafe_project_id(
    home: Path,
) -> None:
    with _client(home) as client:
        for path in (
            "/api/sessions/anything",
            "/api/sessions/anything/segments",
            "/api/sessions/anything/segments/0",
        ):
            r = client.get(path, params={"project_id": "../../etc"})
            assert r.status_code == 422, (path, r.text)
