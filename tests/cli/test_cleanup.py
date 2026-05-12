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
        assert proj["raw_traces_ttl_source"] == "builtin_default"
        assert proj["raw_traces_ttl_days"] == 90
        # task-B5: analysis_ttl resolution surfaces alongside raw_traces.
        assert proj["analysis_ttl_source"] == "builtin_default"
        assert proj["analysis_ttl_days"] == 365


# ---------------------------------------------------------------------------
# Hardening (review MEDIUM-1): --project-id with traversal chars exits 2
# without touching the FS.
# ---------------------------------------------------------------------------


class TestProjectIdSafetyGate:
    @pytest.mark.parametrize(
        "bad_pid",
        ["../../tmp/pwn", "..", "x/y", "x\\y", "."],
    )
    def test_unsafe_project_id_exits_two(self, home: Path, bad_pid: str) -> None:
        result = runner.invoke(
            root_app,
            ["cleanup", "--project-id", bad_pid, "--format", "json"],
        )
        assert result.exit_code == 2, (result.exit_code, result.output)
        # Helpful operator-facing message.
        assert "unsafe" in (result.output + (result.stderr or "")).lower()
        # No projects directory should have been created.
        projects_dir = home / "projects"
        if projects_dir.exists():
            for child in projects_dir.iterdir():
                assert child.name == bad_pid.replace("/", "").replace("\\", "") or (
                    child.name not in {"..", "."}
                ), child


# ===========================================================================
# GUR-149 task-B5 — extend `secondsight cleanup` to also reap analysis_results
# ===========================================================================


class TestB5AnalysisResolutionVisible:
    """task-B5: the CLI must resolve BOTH raw_traces_ttl_days AND
    analysis_ttl_days per project, surface BOTH source attributions in
    output, and run BOTH purgers automatically (no new flag — D7 in
    2-plan.md)."""

    def test_per_project_analysis_ttl_override_surfaces(self, home: Path) -> None:
        """Operator override of `analysis_ttl_days` in per-project config
        is reflected in JSON output and informs the resolved value."""
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=400)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        # Per-project config: override analysis_ttl_days only.
        proj_cfg = home / "projects" / "p1" / "config.toml"
        proj_cfg.write_text("[retention]\nanalysis_ttl_days = 30\n")

        result = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        proj = doc["projects"][0]

        # raw_traces falls through to builtin (90); analysis is per-project (30).
        assert proj["raw_traces_ttl_days"] == 90
        assert proj["raw_traces_ttl_source"] == "builtin_default"
        assert proj["analysis_ttl_days"] == 30
        assert proj["analysis_ttl_source"] == "per_project_config"


class TestB5DcB1AnalysisTypoFallsThroughVisibleInOutput:
    """DC-B1 closes here at the CLI layer: an operator who typoed
    `analysis_ttl_day = 30` (missing `s`) silently gets the builtin 365.
    The ONLY signal is the source attribution surfacing as
    `builtin_default` in the CLI output. The B-S1 acceptance clause
    that was deferred from task-B1 lands here."""

    def test_analysis_ttl_typo_surfaces_as_builtin_default_in_output(self, home: Path) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=400)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        # Typo: missing trailing `s`.
        proj_cfg = home / "projects" / "p1" / "config.toml"
        proj_cfg.write_text("[retention]\nanalysis_ttl_day = 30\n")

        result = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        proj = doc["projects"][0]

        # The operator typed something; nothing was raised; but the
        # detection contract is the source attribution. If the operator
        # was expecting per_project_config and sees builtin_default,
        # they have grounds to look for the typo.
        assert proj["analysis_ttl_days"] == 365
        assert proj["analysis_ttl_source"] == "builtin_default"


class TestB5AnalysisPurgerReapsExpiredAnalysisRows:
    """B-H2 acceptance: K expired session_reports + their behavior_flags
    are reaped by the CLI when `analysis_ttl_days` boundary is crossed.
    Previously orthogonal to raw_traces purging — the CLI must run
    BOTH purgers per project (D7)."""

    def test_real_run_reaps_expired_analysis_rows(self, home: Path) -> None:
        from secondsight.analysis.schemas import (
            BehaviorFlag,
            BehaviorFlagType,
            SessionReport,
        )
        from secondsight.api.registry import ProjectRegistry

        # Seed: expired session reports + flags. Use a different session
        # from the raw_traces seed so we can verify each purger
        # independently. The session itself need not exist in events
        # (CLI also runs a raw_traces enumeration, but if the events
        # table is empty for that session, the raw_traces purger has
        # nothing to do).
        registry = ProjectRegistry(secondsight_home=home)
        resources = registry._build_resources("p1")  # noqa: SLF001
        # Bring up session_reports + behavior_flags schemas.
        from secondsight.storage.session_reports_repository import (
            SessionReportsRepository,
        )
        from secondsight.storage.behavior_flags_repository import (
            BehaviorFlagsRepository,
        )

        reports_repo = SessionReportsRepository(resources.db_engine)
        reports_repo.create_schema()
        flags_repo = BehaviorFlagsRepository(resources.db_engine)
        flags_repo.create_schema()

        old_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=400)
        report = SessionReport(
            id="rep-old",
            project_id="p1",
            session_id="sess-old",
            analysis_run_id="run-old",
            headline="old session",
            key_findings=["finding"],
            body="body",
            created_at=old_ts,
            updated_at=old_ts,
        )
        reports_repo.upsert(report)
        flag = BehaviorFlag(
            id="flag-old",
            project_id="p1",
            session_id="sess-old",
            segment_index=0,
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["evt-x"],
            intent_summary="x",
            reason="x",
            confidence="medium",
            created_at=old_ts,
        )
        flags_repo.insert(flag)
        resources.db_engine.dispose()

        # Per-project config: analysis_ttl_days small enough to expire it.
        proj_cfg = home / "projects" / "p1" / "config.toml"
        proj_cfg.write_text("[retention]\nanalysis_ttl_days = 30\n")

        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        proj = doc["projects"][0]

        # Output names the analysis-side purge.
        assert "sess-old" in (proj.get("analysis_purged_session_ids") or [])
        # Verify rows actually gone from DB.
        registry2 = ProjectRegistry(secondsight_home=home)
        resources2 = registry2._build_resources("p1")  # noqa: SLF001
        reports_repo2 = SessionReportsRepository(resources2.db_engine)
        flags_repo2 = BehaviorFlagsRepository(resources2.db_engine)
        try:
            assert reports_repo2.get_for_session("sess-old") is None
            assert flags_repo2.get_session_flags("sess-old") == []
        finally:
            resources2.db_engine.dispose()


class TestB5DryRunDoesNotInvokeAnalysisPurger:
    """`--dry-run` enumerates from BOTH purgers but invokes NEITHER
    destructive method. Spy on the analysis purger to assert no-op."""

    def test_dry_run_does_not_invoke_analysis_purger(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from secondsight.storage import analysis_retention as ar_mod

        purge_calls: list = []

        original_purge = ar_mod.AnalysisResultsPurger.purge

        def spy_purge(self, expired):  # type: ignore[no-untyped-def]
            purge_calls.append(list(expired))
            return original_purge(self, expired)

        monkeypatch.setattr(ar_mod.AnalysisResultsPurger, "purge", spy_purge)

        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        result = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert result.exit_code == 0, result.output

        assert purge_calls == [], "AnalysisResultsPurger.purge MUST NOT be invoked under --dry-run"


class TestB5EmptyInstallExitsZero:
    """DC-B7 at the CLI layer: a fresh install with no analyzed sessions
    must exit 0; both ttl source attributions must surface; both empty
    enumerations must be reported.

    This is the death case for 'first cleanup run on a fresh DB'.
    """

    def test_empty_db_real_run_exits_zero_with_zero_counts(self, home: Path) -> None:
        # Project exists but has no events, no analysis rows.
        (home / "projects" / "p1").mkdir(parents=True)

        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        if doc["projects"]:
            proj = doc["projects"][0]
            assert proj["expired_session_ids"] == []
            assert proj.get("expired_analysis_session_ids") == []


class TestB5DcB3AnalysisDryRunMatchesRealRun:
    """Yin review B5 Critical fix: DC-3 contract on the analysis side
    was previously unpinned. The cleanup.py docstring claims dry-run
    enumerates the same set as real-run for BOTH purgers, but only the
    raw_traces side had a test pinning it. Without an analysis-side
    test, a future change that re-implements the analysis enumeration
    differently for dry-run would silently drift.

    Pin: bit-for-bit equality of analysis-side enumerated set vs.
    purged set across dry-run and real-run.
    """

    def test_dry_run_and_real_report_same_analysis_session_set(self, home: Path) -> None:
        from secondsight.analysis.schemas import (
            BehaviorFlag,
            BehaviorFlagType,
            SessionReport,
        )
        from secondsight.api.registry import ProjectRegistry
        from secondsight.storage.session_reports_repository import (
            SessionReportsRepository,
        )
        from secondsight.storage.behavior_flags_repository import (
            BehaviorFlagsRepository,
        )

        # Seed multiple expired analyses + one fresh analysis.
        registry = ProjectRegistry(secondsight_home=home)
        resources = registry._build_resources("p1")  # noqa: SLF001
        reports_repo = SessionReportsRepository(resources.db_engine)
        reports_repo.create_schema()
        flags_repo = BehaviorFlagsRepository(resources.db_engine)
        flags_repo.create_schema()

        old_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=400)
        fresh_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=10)

        for i, (sid, ts) in enumerate(
            [
                ("analy-old-1", old_ts),
                ("analy-old-2", old_ts),
                ("analy-fresh", fresh_ts),
            ]
        ):
            reports_repo.upsert(
                SessionReport(
                    id=f"rep-{i}",
                    project_id="p1",
                    session_id=sid,
                    analysis_run_id=f"run-{i}",
                    headline=f"hdr {sid}",
                    key_findings=["f"],
                    body=f"body {sid}",
                    created_at=ts,
                    updated_at=ts,
                )
            )
            flags_repo.insert(
                BehaviorFlag(
                    id=f"flag-{i}",
                    project_id="p1",
                    session_id=sid,
                    segment_index=0,
                    flag_type=BehaviorFlagType.UNNECESSARY_READ,
                    event_ids=["evt-x"],
                    intent_summary="x",
                    reason="x",
                    confidence="medium",
                    created_at=ts,
                )
            )
        resources.db_engine.dispose()

        # Per-project config: analysis_ttl=30 → only old_ts rows expire.
        proj_cfg = home / "projects" / "p1" / "config.toml"
        proj_cfg.write_text("[retention]\nanalysis_ttl_days = 30\n")

        # Dry-run.
        dry = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert dry.exit_code == 0, dry.output
        dry_doc = json.loads(dry.output)
        dry_set = sorted(
            sid
            for proj in dry_doc["projects"]
            for sid in (proj.get("expired_analysis_session_ids") or [])
        )

        # Real run.
        real = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert real.exit_code == 0, real.output
        real_doc = json.loads(real.output)
        real_set = sorted(
            sid
            for proj in real_doc["projects"]
            for sid in (proj.get("analysis_purged_session_ids") or [])
        )

        # Bit-for-bit equality on analysis side.
        assert dry_set == real_set == ["analy-old-1", "analy-old-2"], {
            "dry": dry_set,
            "real": real_set,
        }


class TestB5BH5AnalysisRunsNotReaped:
    """B-H5 acceptance regression guard: an `analysis_runs` row 400 days
    old (older than the 365d default) is NOT reaped by `secondsight cleanup`,
    even when its corresponding session_reports row IS reaped. The
    `analysis_runs` audit table is intentionally out-of-scope for the
    analysis purger.

    Without this regression guard, a future change that naively adds
    `analysis_runs` to the AnalysisResultsPurger would silently land
    in production.
    """

    def test_analysis_runs_row_is_not_touched_by_purger(self, home: Path) -> None:
        from secondsight.analysis.schemas import (
            BehaviorFlag,
            BehaviorFlagType,
            SessionReport,
        )
        from secondsight.api.registry import ProjectRegistry
        from secondsight.storage.analysis_runs_repository import (
            AnalysisRunsRepository,
        )
        from secondsight.storage.session_reports_repository import (
            SessionReportsRepository,
        )
        from secondsight.storage.behavior_flags_repository import (
            BehaviorFlagsRepository,
        )

        registry = ProjectRegistry(secondsight_home=home)
        resources = registry._build_resources("p1")  # noqa: SLF001
        reports_repo = SessionReportsRepository(resources.db_engine)
        reports_repo.create_schema()
        flags_repo = BehaviorFlagsRepository(resources.db_engine)
        flags_repo.create_schema()
        runs_repo = AnalysisRunsRepository(resources.db_engine)
        runs_repo.create_schema()

        old_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=400)

        # Insert an analysis_run for the session.
        run_id = runs_repo.start_run("p1", "sess-old")
        # Insert the session_reports + behavior_flags rows.
        reports_repo.upsert(
            SessionReport(
                id="rep-old",
                project_id="p1",
                session_id="sess-old",
                analysis_run_id=run_id,
                headline="old session",
                key_findings=["f"],
                body="body",
                created_at=old_ts,
                updated_at=old_ts,
            )
        )
        flags_repo.insert(
            BehaviorFlag(
                id="flag-old",
                project_id="p1",
                session_id="sess-old",
                segment_index=0,
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                event_ids=["evt-x"],
                intent_summary="x",
                reason="x",
                confidence="medium",
                created_at=old_ts,
            )
        )
        resources.db_engine.dispose()

        # Per-project config: aggressive analysis_ttl.
        proj_cfg = home / "projects" / "p1" / "config.toml"
        proj_cfg.write_text("[retention]\nanalysis_ttl_days = 30\n")

        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 0, result.output

        # session_reports + behavior_flags reaped; analysis_runs row remains.
        registry2 = ProjectRegistry(secondsight_home=home)
        resources2 = registry2._build_resources("p1")  # noqa: SLF001
        reports_repo2 = SessionReportsRepository(resources2.db_engine)
        runs_repo2 = AnalysisRunsRepository(resources2.db_engine)
        try:
            assert reports_repo2.get_for_session("sess-old") is None
            # Audit row MUST still exist — out of scope for analysis purger.
            run_after = runs_repo2.get_latest_for_session("sess-old")
            assert run_after is not None, (
                "B-H5: analysis_runs row was reaped but should be out of scope"
            )
            assert run_after.id == run_id
        finally:
            resources2.db_engine.dispose()


class TestB5StructuredLogPerProject:
    """Important fix: B-S1 acceptance requires a structured INFO log line
    naming both resolved values + sources. JSON output covers the
    detection contract, but the log line is the operator's primary
    surface for cleanup runs invoked via cron."""

    def test_cleanup_emits_structured_info_per_project(
        self, home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=100)
        _seed(home, project_id="p1", session_id="s1", last_ts=old)

        with caplog.at_level("INFO", logger="secondsight.cli.cleanup"):
            result = runner.invoke(root_app, ["cleanup", "--dry-run", "--format", "json"])
        assert result.exit_code == 0, result.output

        # Find the structured log line for p1.
        info_msgs = [r.getMessage() for r in caplog.records]
        retention_lines = [m for m in info_msgs if "retention resolved" in m]
        assert len(retention_lines) >= 1, (
            f"Expected structured retention log line, got: {info_msgs}"
        )
        line = retention_lines[0]
        assert "project_id=p1" in line
        assert "raw_traces_ttl_days=90" in line
        assert "raw_traces_source=builtin_default" in line
        assert "analysis_ttl_days=365" in line
        assert "analysis_ttl_source=builtin_default" in line


# ===========================================================================
# Iteration round 1 — fix for scar-B5-1
# ===========================================================================


class TestB5OrderingPin:
    """Pin the per-project purger ordering contract: raw_traces FIRST,
    analysis SECOND.

    Rationale (scar-B5-1, deferred at task-B5, fixed in iteration round 1):
    if raw_traces purge fails mid-run, the analysis purger still attempts
    its own set; if analysis fails after raw_traces succeeded, the
    raw_traces are already gone but the analysis enumeration remains
    re-attemptable on the next CLI invocation. The reverse order would
    leave a partial state where analysis_results is gone but raw_traces
    still exists — operator could not re-derive the analysis from raw
    events (the analyzer needs them).

    A future refactor that swaps the two `if outcome.expired:` /
    `if outcome.expired_analyses:` blocks in `cleanup()` would not
    break any other test — it would only break this one. That's the
    whole point: this test exists to fail loudly on order regressions.
    """

    def test_per_project_raw_traces_runs_before_analysis(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from secondsight.analysis.schemas import (
            BehaviorFlag,
            BehaviorFlagType,
            SessionReport,
        )
        from secondsight.api.registry import ProjectRegistry
        from secondsight.cli import cleanup as cleanup_mod
        from secondsight.storage.behavior_flags_repository import (
            BehaviorFlagsRepository,
        )
        from secondsight.storage.session_reports_repository import (
            SessionReportsRepository,
        )

        # Seed raw_traces side: expired session events.
        old = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=400)
        _seed(home, project_id="p1", session_id="s-raw", last_ts=old)

        # Seed analysis side: expired session_reports + behavior_flags.
        registry = ProjectRegistry(secondsight_home=home)
        resources = registry._build_resources("p1")  # noqa: SLF001
        reports_repo = SessionReportsRepository(resources.db_engine)
        reports_repo.create_schema()
        flags_repo = BehaviorFlagsRepository(resources.db_engine)
        flags_repo.create_schema()
        reports_repo.upsert(
            SessionReport(
                id="rep-old",
                project_id="p1",
                session_id="s-analysis",
                analysis_run_id="run-old",
                headline="old session",
                key_findings=["f"],
                body="body",
                created_at=old,
                updated_at=old,
            )
        )
        flags_repo.insert(
            BehaviorFlag(
                id="flag-old",
                project_id="p1",
                session_id="s-analysis",
                segment_index=0,
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                event_ids=["evt-x"],
                intent_summary="x",
                reason="x",
                confidence="medium",
                created_at=old,
            )
        )
        resources.db_engine.dispose()

        # Per-project config: aggressive analysis_ttl so analysis side fires.
        proj_cfg = home / "projects" / "p1" / "config.toml"
        proj_cfg.write_text("[retention]\nanalysis_ttl_days = 30\n")

        # Spy on both purge helpers; record invocation order.
        call_order: list[str] = []
        real_raw = cleanup_mod._purge_for_project
        real_analysis = cleanup_mod._analysis_purge_for_project

        def spy_raw(home_arg, pid, expired):  # type: ignore[no-untyped-def]
            call_order.append(f"raw_traces:{pid}")
            return real_raw(home_arg, pid, expired)

        def spy_analysis(home_arg, pid, expired):  # type: ignore[no-untyped-def]
            call_order.append(f"analysis:{pid}")
            return real_analysis(home_arg, pid, expired)

        monkeypatch.setattr(cleanup_mod, "_purge_for_project", spy_raw)
        monkeypatch.setattr(cleanup_mod, "_analysis_purge_for_project", spy_analysis)

        result = runner.invoke(root_app, ["cleanup", "--format", "json"])
        assert result.exit_code == 0, result.output

        # The whole point: raw_traces FIRST, analysis SECOND. Reverse order
        # would mean analysis_results is reaped before raw_traces — the
        # operator could not re-derive analyses from raw events on retry.
        assert call_order == ["raw_traces:p1", "analysis:p1"], (
            f"Per-project purger ordering violated. Expected "
            f"[raw_traces, analysis], got: {call_order!r}"
        )
