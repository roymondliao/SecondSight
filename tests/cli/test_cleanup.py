"""Death tests + happy-path tests for `secondsight cleanup` CLI (task-A6).

Death case from
``changes/2026-05-06_gur-107_phase3a-retention-observation-api/2-plan.md`` §5:
    DC-3: ``--dry-run`` calling the same enumerator path as a real run
          produces an identical session set (no enumeration drift —
          see plan D3).

If dry-run silently used a different code path, an operator could
preview "0 sessions" yet a subsequent real run could reap several. Or
the inverse: preview "5 sessions" and reap none. Either drift makes
``--dry-run`` worse than useless. We pin DC-3 by running both paths
against the same seeded fixture and asserting bit-for-bit equality on
the reported session set.

We also pin:
- DC-5 propagation: a purge that reports failures must exit 1 from the
  CLI so scripts notice.
- ``--project-id`` scopes to one project (no cross-project reap).
- Empty home → exit 0, zero-project report.
- Default TTL (built-in 90 days) is honoured when no config file exists.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from secondsight.cli.app import app as root_app
from secondsight.event import EventType
from tests.conftest import make_event

UTC = timezone.utc
runner = CliRunner()


# ---------------------------------------------------------------------------
# Seed helpers (mirror the API router test pattern; FS-walk ready)
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    monkeypatch.setenv("SECONDSIGHT_HOME", str(h))
    return h


def _seed(
    home: Path,
    *,
    project_id: str,
    session_id: str,
    last_ts: datetime,
    event_count: int = 1,
) -> None:
    """Materialize a project under home/projects/<pid>/ with FS + DB events.

    The CLI walks this directory directly (no async registry), mirroring
    `cli/sync.py:_select_project_ids`.
    """
    from secondsight.api.registry import ProjectRegistry

    registry = ProjectRegistry(secondsight_home=home)
    resources = registry._build_resources(project_id)  # noqa: SLF001

    async def _write_all() -> None:
        for i in range(event_count):
            ts = last_ts - timedelta(seconds=event_count - 1 - i)
            ev = make_event(
                event_id=f"{session_id}-{i:04d}",
                session_id=session_id,
                project_id=project_id,
                sequence_number=i,
                timestamp=ts,
                event_type=EventType.USER_PROMPT,
            )
            resources.events_repository.insert(ev)
            await resources.raw_trace_store.write(ev)

    asyncio.run(_write_all())
    resources.db_engine.dispose()


def _session_dir(home: Path, project_id: str, session_id: str) -> Path:
    return home / "projects" / project_id / "sessions" / session_id


def _project_db_count(home: Path, project_id: str, session_id: str) -> int:
    """Count events in the project's DB for a given session_id."""
    from secondsight.storage.db_engine import DBEngine
    import sqlalchemy as sa
    from secondsight.storage.events_table import events as events_table

    db = DBEngine(db_path=home / "projects" / project_id / "intelligence.db")
    try:
        with db.engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(events_table)
                    .where(events_table.c.session_id == session_id)
                ).scalar()
                or 0
            )
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# DC-3: --dry-run uses identical enumeration path as real run.
# ---------------------------------------------------------------------------


class TestDC3DryRunMatchesRealRun:
    def test_dry_run_and_real_report_same_session_set(self, home: Path) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)
        _seed(home, project_id="p1", session_id="s2", last_ts=old)
        # Fresh session (NOT expired) — must be excluded both runs.
        fresh = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=10)
        _seed(home, project_id="p1", session_id="s3", last_ts=fresh)

        # Dry-run first.
        dry = runner.invoke(
            root_app,
            ["cleanup", "--dry-run", "--format", "json"],
        )
        assert dry.exit_code == 0, dry.output
        dry_doc = json.loads(dry.output)
        dry_sessions = sorted(
            sid for proj in dry_doc["projects"] for sid in proj["expired_session_ids"]
        )

        # Real run.
        real = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert real.exit_code == 0, real.output
        real_doc = json.loads(real.output)
        real_sessions = sorted(
            sid for proj in real_doc["projects"] for sid in proj["purged_session_ids"]
        )

        # Bit-for-bit equality on the session set both paths report.
        assert dry_sessions == real_sessions == ["s1", "s2"], {
            "dry": dry_sessions,
            "real": real_sessions,
        }

    def test_dry_run_does_not_delete(self, home: Path) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        result = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert result.exit_code == 0, result.output

        # Both sides intact.
        assert _session_dir(home, "p1", "s1").exists()
        assert _project_db_count(home, "p1", "s1") == 1

    def test_real_run_deletes_both_sides(self, home: Path) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old, event_count=2)

        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 0, result.output

        assert not _session_dir(home, "p1", "s1").exists()
        assert _project_db_count(home, "p1", "s1") == 0


# ---------------------------------------------------------------------------
# Project scoping.
# ---------------------------------------------------------------------------


class TestProjectScoping:
    def test_project_id_flag_scopes_to_one_project(self, home: Path) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="alpha", session_id="sa", last_ts=old)
        _seed(home, project_id="beta", session_id="sb", last_ts=old)

        result = runner.invoke(
            root_app,
            ["cleanup", "--project-id", "alpha", "--format", "json"],
        )
        assert result.exit_code == 0, result.output

        # alpha cleared, beta untouched.
        assert not _session_dir(home, "alpha", "sa").exists()
        assert _session_dir(home, "beta", "sb").exists()

    def test_empty_home_exits_zero(self, home: Path) -> None:
        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["projects"] == []


# ---------------------------------------------------------------------------
# DC-5 propagation: purge failures surface as non-zero exit.
# ---------------------------------------------------------------------------


class TestPurgeFailureExit:
    def test_db_failure_during_purge_exits_one(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        from secondsight.storage import retention as ret_mod
        import sqlalchemy as sa

        original = ret_mod._delete_db_events_for_session

        def boom(repo, session_id):
            raise sa.exc.OperationalError("DELETE", {}, Exception("synthetic"))

        monkeypatch.setattr(ret_mod, "_delete_db_events_for_session", boom)

        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 1, result.output

        # Restore so other tests don't suffer.
        monkeypatch.setattr(ret_mod, "_delete_db_events_for_session", original)


# ---------------------------------------------------------------------------
# RetentionConfig source attribution surfaces.
# ---------------------------------------------------------------------------


class TestConfigSourceVisible:
    def test_default_ttl_source_is_builtin_default(self, home: Path) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        result = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        proj = doc["projects"][0]
        assert proj["ttl_source"] == "builtin_default"
        assert proj["raw_traces_ttl_days"] == 90
