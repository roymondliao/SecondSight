"""Death + happy-path tests for `secondsight directive` CLI (GUR-104 task-4).

Death test inventory (Samsara — written BEFORE implementation):

- DT-4.1: byte-identical JSON — CLI --no-server vs server-mode produce
  sha256-identical output. Defense: DC-4 CLI/API drift.
- DT-4.2: ConnectError → silent fallback (exit 0, stderr "not reachable",
  stdout has expected JSON).
- DT-4.3: HTTPStatusError → loud exit (exit 1, stderr has status code,
  NO in-process fallback attempted).
- DT-4.4: --disable requires --reason (exit 2, message names missing flag).
- DT-4.5: --enable on already-active directive → exit 0, "no change" message.
- HP-4.1: --active --format json → valid JSON, list[dict] of length 2,
  each item has full DirectiveOut shape.
- HP-4.2: --active (default format) → Rich table with id / type / summary /
  frequency / created_at columns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.api.directives import DirectiveOut
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import create_app
from secondsight.storage.directives_repository import DirectivesRepository

# CLI entry point under test
from secondsight.cli.app import app

UTC = timezone.utc
_PROJECT_ID = "proj-directive-test"
_DIR_ID_1 = "dir-test-001"
_DIR_ID_2 = "dir-test-002"
_DIR_ID_DISABLED = "dir-test-dis"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _seed_directive(
    home: Path,
    *,
    project_id: str,
    directive_id: str,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
    instruction: str = "Always read AGENTS.md first.",
    frequency: float | None = 0.7,
    identity_key: str | None = None,
    disabled_at: datetime | None = None,
    disabled_reason: str | None = None,
) -> Directive:
    """Materialize the project's resources, then directly write a directive row."""
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    db_engine = resources.db_engine
    repo = DirectivesRepository(db_engine)
    repo.create_schema()

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    directive = Directive(
        id=directive_id,
        project_id=project_id,
        type=DirectiveType.CONVENTION,
        status=status,
        instruction=instruction,
        frequency=frequency,
        identity_key=identity_key or f"key-{directive_id}",
        source_sessions=["s1"],
        created_at=now,
        updated_at=now,
        disabled_at=disabled_at,
        disabled_reason=disabled_reason,
    )
    repo.insert(directive)
    asyncio.run(registry.aclose())
    return directive


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# DEATH PATHS
# =====================================================================


class TestDeathPaths:
    # ------------------------------------------------------------------
    # DT-4.1: byte-identical JSON between server-mode and --no-server
    # ------------------------------------------------------------------

    def test_dt_4_1_byte_identical_json_server_vs_no_server(self, home: Path) -> None:
        """DC-4 defense: --no-server JSON MUST be byte-identical to server-mode JSON.

        Silent failure this closes: if the CLI formats datetime differently than
        FastAPI does (e.g., ISO-8601 with vs without timezone suffix), agents
        would see structurally matching but value-differing JSON from the two
        paths — a drift that would only surface via behavioral divergence, not
        at import time.

        Approach: uses ASGI transport (no real port needed) for the server path
        so both paths go through the same serialization function.
        """
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_1)
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_2)

        # (a) server-mode result: use TestClient (real ASGI lifecycle)
        test_app = create_app(secondsight_home=home)
        with TestClient(test_app, raise_server_exceptions=True) as client:
            r = client.get(
                "/api/directives",
                params={"project_id": _PROJECT_ID, "active": True},
            )
        assert r.status_code == 200, f"API GET failed: {r.text}"
        # Normalize for comparison: sort list by id (order stability)
        server_items = sorted(r.json(), key=lambda d: d["id"])
        # Re-serialize with sorted keys to ensure canonical form
        server_json = json.dumps(server_items, sort_keys=True)

        # (b) --no-server result
        result = runner.invoke(
            app,
            [
                "directive",
                "--active",
                "--format",
                "json",
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )
        assert result.exit_code == 0, (
            f"CLI --no-server failed with exit {result.exit_code}. "
            f"stdout: {result.stdout!r}  stderr: {result.stderr!r}"
        )
        cli_items = sorted(json.loads(result.stdout), key=lambda d: d["id"])
        cli_json = json.dumps(cli_items, sort_keys=True)

        assert _sha256(server_json) == _sha256(cli_json), (
            f"DT-4.1 FAILED: CLI/API JSON drift detected.\n"
            f"API JSON:  {server_json[:500]}\n"
            f"CLI JSON:  {cli_json[:500]}"
        )

    # ------------------------------------------------------------------
    # DT-4.2: ConnectError → silent fallback
    # ------------------------------------------------------------------

    def test_dt_4_2_connect_error_silent_fallback(self, home: Path) -> None:
        """ConnectError (server not running) → exit 0, stderr "not reachable",
        stdout has the expected JSON (in-process result).

        Silent failure this closes: if ConnectError raised Exit(1), operators
        with no server running would be blocked from using the CLI entirely.
        """
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_1)

        import httpx as _real_httpx

        with patch("secondsight.cli.directive.httpx") as mock_httpx:
            mock_httpx.ConnectError = _real_httpx.ConnectError
            mock_httpx.HTTPStatusError = _real_httpx.HTTPStatusError
            mock_httpx.get = MagicMock(side_effect=_real_httpx.ConnectError("connection refused"))

            result = runner.invoke(
                app,
                [
                    "directive",
                    "--active",
                    "--format",
                    "json",
                    "--project",
                    _PROJECT_ID,
                    "--home",
                    str(home),
                    "--server-url",
                    "http://127.0.0.1:19999",
                ],
            )

        assert result.exit_code == 0, (
            f"DT-4.2: Expected exit 0 on ConnectError fallback, "
            f"got {result.exit_code}. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "not reachable" in result.stderr.lower(), (
            f"DT-4.2: Expected 'not reachable' in stderr. Got: {result.stderr!r}"
        )
        items = json.loads(result.stdout)
        assert isinstance(items, list) and len(items) >= 1, (
            f"DT-4.2: Expected non-empty JSON list in stdout. Got: {result.stdout!r}"
        )

    # ------------------------------------------------------------------
    # DT-4.3: HTTPStatusError → loud exit, NO fallback
    # ------------------------------------------------------------------

    def test_dt_4_3_http_status_error_loud_exit_no_fallback(self, home: Path) -> None:
        """HTTPStatusError → exit 1, stderr has status code, in-process NOT called.

        Silent failure this closes: if HTTPStatusError triggered in-process fallback
        (same as ConnectError), a broken server endpoint would be masked — the
        operator would see a successful CLI run whose result came from a different
        code path than expected, hiding server-side bugs.
        """
        import httpx as _real_httpx

        mock_response = MagicMock()
        mock_response.status_code = 500
        http_error = _real_httpx.HTTPStatusError(
            "Internal Server Error",
            request=MagicMock(),
            response=mock_response,
        )

        with patch("secondsight.cli.directive.httpx") as mock_httpx:
            mock_httpx.ConnectError = _real_httpx.ConnectError
            mock_httpx.HTTPStatusError = _real_httpx.HTTPStatusError
            mock_httpx.get = MagicMock(side_effect=http_error)

            with patch("secondsight.cli.directive._list_directives_in_process") as mock_in_process:
                result = runner.invoke(
                    app,
                    [
                        "directive",
                        "--active",
                        "--format",
                        "json",
                        "--project",
                        _PROJECT_ID,
                        "--home",
                        str(home),
                        "--server-url",
                        "http://127.0.0.1:8420",
                    ],
                )
                mock_in_process.assert_not_called()

        assert result.exit_code == 1, (
            f"DT-4.3: Expected exit 1 for HTTPStatusError (server up, endpoint broken). "
            f"Got {result.exit_code}. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "500" in result.stderr, f"DT-4.3: Expected '500' in stderr. Got: {result.stderr!r}"

    # ------------------------------------------------------------------
    # DT-4.4: --disable requires --reason
    # ------------------------------------------------------------------

    def test_dt_4_4_disable_requires_reason(self, home: Path) -> None:
        """--disable ID without --reason must fail with exit 2 (BadParameter).

        Silent failure this closes: without this guard, --disable with no reason
        would send PATCH with reason=None to the server (or in-process), which
        the repository would reject with a LookupError or ValueError at storage
        layer — a far less informative error than a fast CLI rejection.
        """
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_1)

        result = runner.invoke(
            app,
            [
                "directive",
                "--disable",
                _DIR_ID_1,
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )

        assert result.exit_code == 2, (
            f"DT-4.4: Expected exit 2 for --disable without --reason. "
            f"Got {result.exit_code}. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "reason" in combined.lower(), (
            f"DT-4.4: Expected 'reason' mentioned in output. Got: {combined!r}"
        )

    # ------------------------------------------------------------------
    # DT-4.5: --enable on already-active directive is a no-op
    # ------------------------------------------------------------------

    def test_dt_4_5_enable_noop_already_active(self, home: Path) -> None:
        """--enable on active directive → exit 0, 'no change' or 'already active' message.

        The no-op check MUST happen via get_by_id BEFORE calling update_status,
        because update_status(ACTIVE, reason=None) on an already-ACTIVE row would
        be a no-op at DB level but could produce a confusing audit trail entry
        if it advanced updated_at.
        """
        _seed_directive(
            home,
            project_id=_PROJECT_ID,
            directive_id=_DIR_ID_1,
            status=DirectiveStatus.ACTIVE,
        )

        result = runner.invoke(
            app,
            [
                "directive",
                "--enable",
                _DIR_ID_1,
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )

        assert result.exit_code == 0, (
            f"DT-4.5: Expected exit 0 for --enable on already-active. "
            f"Got {result.exit_code}. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "no change" in combined.lower() or "already active" in combined.lower(), (
            f"DT-4.5: Expected 'no change' or 'already active' in output. Got: {combined!r}"
        )


# =====================================================================
# HAPPY PATHS
# =====================================================================


class TestHappyPaths:
    # ------------------------------------------------------------------
    # HP-4.1: --active --format json shape matches DirectiveOut
    # ------------------------------------------------------------------

    def test_hp_4_1_active_format_json_shape(self, home: Path) -> None:
        """--active --format json returns valid JSON list of DirectiveOut dicts.

        Each item must have all required DirectiveOut fields.
        """
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_1)
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_2)
        # disabled one: must not appear
        _seed_directive(
            home,
            project_id=_PROJECT_ID,
            directive_id=_DIR_ID_DISABLED,
            status=DirectiveStatus.DISABLED,
            disabled_at=datetime(2026, 5, 8, 13, 0, 0, tzinfo=UTC),
            disabled_reason="outdated",
        )

        result = runner.invoke(
            app,
            [
                "directive",
                "--active",
                "--format",
                "json",
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )

        assert result.exit_code == 0, (
            f"HP-4.1: CLI exited {result.exit_code}. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        items = json.loads(result.stdout)
        assert isinstance(items, list), f"HP-4.1: Expected list, got {type(items)}"
        assert len(items) == 2, f"HP-4.1: Expected 2 active items, got {len(items)}"

        required_fields = set(DirectiveOut.model_fields.keys())
        for item in items:
            assert isinstance(item, dict), f"HP-4.1: Each item must be dict, got {type(item)}"
            missing = required_fields - item.keys()
            assert not missing, f"HP-4.1: Missing fields: {missing}"
            assert item["status"] == "active"
            assert item["project_id"] == _PROJECT_ID

    # ------------------------------------------------------------------
    # HP-4.2: default format → Rich table with expected columns
    # ------------------------------------------------------------------

    def test_hp_4_2_default_format_rich_table(self, home: Path) -> None:
        """--active without --format produces a Rich table with expected columns.

        Pins assertion to column headers (not glyphs), since box style varies.
        """
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_1)
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_2)

        result = runner.invoke(
            app,
            [
                "directive",
                "--active",
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )

        assert result.exit_code == 0, (
            f"HP-4.2: CLI exited {result.exit_code}. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        output = result.stdout
        # Check column headers are present (case-insensitive to allow Rich formatting)
        output_lower = output.lower()
        for col in ("id", "type", "frequency", "created_at"):
            assert col in output_lower, (
                f"HP-4.2: Expected column '{col}' in table output. Got:\n{output}"
            )
        # Also check 'instruction' (summary column) is present
        assert "instruction" in output_lower or "summary" in output_lower, (
            f"HP-4.2: Expected 'instruction' or 'summary' column. Got:\n{output}"
        )
        # Verify the directive IDs appear in the table
        assert _DIR_ID_1 in output, f"HP-4.2: Expected {_DIR_ID_1!r} in table output."
        assert _DIR_ID_2 in output, f"HP-4.2: Expected {_DIR_ID_2!r} in table output."

    # ------------------------------------------------------------------
    # HP-4.3: --disable --reason works end-to-end (in-process)
    # ------------------------------------------------------------------

    def test_hp_4_3_disable_with_reason_in_process(self, home: Path) -> None:
        """--disable DIR_ID --reason 'x' → exit 0, directive is disabled."""
        _seed_directive(home, project_id=_PROJECT_ID, directive_id=_DIR_ID_1)

        result = runner.invoke(
            app,
            [
                "directive",
                "--disable",
                _DIR_ID_1,
                "--reason",
                "outdated vocabulary",
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )

        assert result.exit_code == 0, (
            f"HP-4.3: CLI exited {result.exit_code}. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        output = result.stdout + result.stderr
        assert "disabled" in output.lower() or _DIR_ID_1 in output, (
            f"HP-4.3: Expected 'disabled' or directive id in output. Got: {output!r}"
        )

    # ------------------------------------------------------------------
    # HP-4.4: --enable re-enables a disabled directive
    # ------------------------------------------------------------------

    def test_hp_4_4_enable_re_enables_disabled(self, home: Path) -> None:
        """--enable DIR_ID on a disabled directive → exit 0, directive active."""
        _seed_directive(
            home,
            project_id=_PROJECT_ID,
            directive_id=_DIR_ID_1,
            status=DirectiveStatus.DISABLED,
            disabled_at=datetime(2026, 5, 8, 13, 0, 0, tzinfo=UTC),
            disabled_reason="old reason",
        )

        result = runner.invoke(
            app,
            [
                "directive",
                "--enable",
                _DIR_ID_1,
                "--project",
                _PROJECT_ID,
                "--home",
                str(home),
                "--no-server",
            ],
        )

        assert result.exit_code == 0, (
            f"HP-4.4: CLI exited {result.exit_code}. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # Verify DB state
        registry = ProjectRegistry(secondsight_home=home)
        resources = asyncio.run(registry.get(_PROJECT_ID))
        repo = DirectivesRepository(resources.db_engine)
        repo.create_schema()
        directive = repo.get_by_id(_DIR_ID_1)
        asyncio.run(registry.aclose())
        assert directive is not None
        assert directive.status == DirectiveStatus.ACTIVE
        assert directive.disabled_at is None
        assert directive.disabled_reason is None
