"""Death + unit tests for FilesystemBackfill (GUR-98 / P1-13).

Death tests:

  DT-1  sync_log replay: a sync_log entry whose raw_trace file is corrupt
        is NOT silently dropped — it stays in sync_log AND the failure
        surfaces in BackfillReport.failures. Dropping it would erase the
        operator's only record of the failure.

  DT-2  sync_log replay is idempotent on event_id: re-running after a
        successful insert does not double-insert (relies on EventsRepository
        ON CONFLICT DO NOTHING) and does not leave stale sync_log entries.

  DT-3  Filesystem walk picks up an event JSON that is on disk but absent
        from the DB and INSERTs it. Without this, an rsync'd raw trace
        would silently never reach analysis.

  DT-4  archive_fallback_events MUST move-aside (not delete) the live
        file. A regression that unlinked the file before the operator
        could inspect it would erase pending work.

  DT-5  archive_fallback_events handles same-second double-archive: two
        rapid syncs do NOT clobber each other's .bak files. Without
        this, the second sync would silently overwrite the first's
        archive (data loss).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from secondsight.api.registry import ProjectRegistry
from secondsight.event import Event, EventType
from secondsight.storage.filesystem_backfill import (
    FilesystemBackfill,
    archive_fallback_events,
)


def _make_event(event_id: str = "evt-1", session_id: str = "sess-A", seq: int = 0) -> Event:
    return Event(
        id=event_id,
        session_id=session_id,
        project_id="pid",
        event_type=EventType.USER_PROMPT,
        timestamp=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
        sequence_number=seq,
        segment_index=0,
        data={"prompt_length": 5},
    )


@pytest.fixture
def project_resources(tmp_path: Path):
    """Build a real ProjectResources rooted under tmp_path."""
    home = tmp_path / "ss"
    registry = ProjectRegistry(secondsight_home=home)
    return registry._build_resources("pid")  # noqa: SLF001


# ---------------------------------------------------------------------------
# DT-1: missing AND corrupt raw_trace -> failure surfaced, entry NOT dropped
# ---------------------------------------------------------------------------


def test_death_missing_raw_trace_keeps_sync_log_entry(project_resources) -> None:
    """Missing file -> exercises the OSError branch of _replay_sync_log."""
    sync_log = project_resources.sync_log
    bogus_path = Path("/tmp/secondsight-test-does-not-exist.json")
    sync_log.record_failure(
        event_id="evt-x",
        raw_trace_path=bogus_path,
        error=RuntimeError("simulated"),
    )

    backfill = FilesystemBackfill(project_resources)
    report = backfill.run()

    assert report.sync_log_replayed == 0
    assert report.sync_log_remaining == 1, (
        "missing-file entries must remain in sync_log so operator sees them"
    )
    assert any("evt-x" in f for f in report.failures), (
        f"failure must surface evt-x, got {report.failures!r}"
    )
    assert sync_log.path.is_file()
    assert sum(1 for _ in sync_log.path.open()) == 1


def test_death_corrupt_json_raw_trace_keeps_sync_log_entry(
    project_resources, tmp_path: Path
) -> None:
    """File present but not valid JSON -> exercises the
    RawTraceCorruptionError branch of _replay_sync_log.

    Without this test, removing the `except RawTraceCorruptionError` arm
    in production would leave the OSError fallback alone and pass the
    earlier missing-file death test, hiding the regression. Cited as
    coverage gap #1 in GUR-98 review.
    """
    sync_log = project_resources.sync_log
    corrupt_path = tmp_path / "corrupt-trace.json"
    corrupt_path.write_text("{ this is not valid JSON", encoding="utf-8")
    sync_log.record_failure(
        event_id="evt-corrupt",
        raw_trace_path=corrupt_path,
        error=RuntimeError("orig"),
    )

    report = FilesystemBackfill(project_resources).run()

    assert report.sync_log_replayed == 0
    assert report.sync_log_remaining == 1
    assert any("evt-corrupt" in f for f in report.failures), (
        f"corrupt-JSON failure must surface evt-corrupt, got {report.failures!r}"
    )
    # The corrupt file is still on disk — we never auto-deleted it.
    assert corrupt_path.is_file()


def test_death_corrupt_json_in_filesystem_walk_does_not_abort(
    project_resources,
) -> None:
    """A corrupt event JSON inside sessions/.../events/ must not abort the
    walk — it should be recorded in failures and iteration continues to
    surface the rest of that session's events. Coverage gap #1 (walk-side).
    """
    repo = project_resources.events_repository
    store = project_resources.raw_trace_store

    good = _make_event(event_id="walk-good")
    good_path = store.event_path(good)
    good_path.parent.mkdir(parents=True, exist_ok=True)
    good_path.write_text(good.model_dump_json(), encoding="utf-8")

    # Drop a corrupt sibling next to the valid event.
    corrupt = good_path.parent / "20260505T120001000Z_user_prompt_seq000099.json"
    corrupt.write_text("not json", encoding="utf-8")

    report = FilesystemBackfill(project_resources).run()
    assert report.filesystem_inserted == 1, "valid event must still be inserted"
    assert repo.exists(good.id)
    assert any(str(corrupt) in f for f in report.failures), (
        f"corrupt sibling must surface in failures, got {report.failures!r}"
    )


# ---------------------------------------------------------------------------
# DT-2: replay is idempotent
# ---------------------------------------------------------------------------


def test_death_sync_log_replay_idempotent(project_resources) -> None:
    repo = project_resources.events_repository
    store = project_resources.raw_trace_store
    sync_log = project_resources.sync_log

    # Write an event to the filesystem, record a sync_log entry as if
    # the original DB INSERT had failed.
    event = _make_event()
    path = store.event_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(event.model_dump_json(), encoding="utf-8")
    sync_log.record_failure(
        event_id=event.id,
        raw_trace_path=path,
        error=RuntimeError("orig"),
    )

    # First run: replays into DB and clears sync_log.
    report = FilesystemBackfill(project_resources).run()
    assert report.sync_log_replayed == 1
    assert report.sync_log_remaining == 0
    assert not sync_log.path.exists(), "empty sync_log should be removed"
    assert repo.exists(event.id)

    # Second run: already inserted; sync_log is empty; nothing to do.
    report2 = FilesystemBackfill(project_resources).run()
    assert report2.sync_log_replayed == 0
    assert report2.sync_log_remaining == 0


# ---------------------------------------------------------------------------
# DT-3: filesystem walk picks up missing rows
# ---------------------------------------------------------------------------


def test_death_filesystem_walk_inserts_missing_event(project_resources) -> None:
    repo = project_resources.events_repository
    store = project_resources.raw_trace_store

    event = _make_event(event_id="orphan-1")
    path = store.event_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(event.model_dump_json(), encoding="utf-8")

    assert not repo.exists(event.id)
    report = FilesystemBackfill(project_resources).run()
    assert report.filesystem_inserted == 1
    assert repo.exists(event.id)


def test_filesystem_walk_skips_already_present(project_resources) -> None:
    repo = project_resources.events_repository
    store = project_resources.raw_trace_store

    event = _make_event(event_id="evt-already-there")
    path = store.event_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(event.model_dump_json(), encoding="utf-8")
    repo.insert(event)

    report = FilesystemBackfill(project_resources).run()
    assert report.filesystem_inserted == 0
    assert report.filesystem_already_present == 1


# ---------------------------------------------------------------------------
# DT-4: archive moves aside, never deletes
# ---------------------------------------------------------------------------


def test_death_archive_moves_aside_not_deletes(tmp_path: Path) -> None:
    fb = tmp_path / "fallback_events.jsonl"
    fb.write_text(
        json.dumps({"agent": "claude_code", "event_type": "session_start"}) + "\n",
        encoding="utf-8",
    )
    report = archive_fallback_events(fb)

    assert report.archived is True
    assert report.archive_path is not None
    assert report.archive_path.exists()
    assert not fb.exists(), "live fallback file must be moved away"
    # Archive content matches what was there before.
    obj = json.loads(report.archive_path.read_text(encoding="utf-8").splitlines()[0])
    assert obj["agent"] == "claude_code"


def test_archive_no_op_when_file_missing(tmp_path: Path) -> None:
    report = archive_fallback_events(tmp_path / "nope.jsonl")
    assert report.archived is False
    assert report.line_count == 0


def test_archive_no_op_when_file_empty(tmp_path: Path) -> None:
    fb = tmp_path / "fallback_events.jsonl"
    fb.write_text("", encoding="utf-8")
    report = archive_fallback_events(fb)
    assert report.archived is False
    assert report.error is None
    assert fb.exists(), "empty file must NOT be moved (no work to archive)"


def test_death_archive_surfaces_oserror_on_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError while counting lines must NOT silently degrade to
    archived=False with no signal — the operator needs to know the
    fallback file exists but couldn't be archived. GUR-98 review C2.
    """
    fb = tmp_path / "fallback_events.jsonl"
    fb.write_text("one line\n", encoding="utf-8")

    real_open = Path.open

    def open_failing(self, *args, **kwargs):
        if self == fb:
            raise PermissionError("simulated permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_failing)

    report = archive_fallback_events(fb)
    assert report.archived is False
    assert report.error is not None, (
        "unreadable fallback file must surface a structured error, "
        "not silently return archived=False"
    )
    assert "PermissionError" in report.error
    # Live file still on disk — we did not move it.
    assert fb.exists()


# ---------------------------------------------------------------------------
# DT-5: same-second double archive does not collide
# ---------------------------------------------------------------------------


def test_death_same_second_double_archive_no_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fb = tmp_path / "fallback_events.jsonl"

    # Freeze the timestamp used for the .bak suffix so both archive calls
    # would otherwise produce the same .bak path.
    fixed = "20260505T120000Z"
    monkeypatch.setattr(
        "secondsight.storage.filesystem_backfill.time.strftime",
        lambda fmt, t=None: fixed,
    )

    fb.write_text("first\n", encoding="utf-8")
    first = archive_fallback_events(fb)
    assert first.archived
    assert first.archive_path is not None
    first_bytes = first.archive_path.read_bytes()

    fb.write_text("second\n", encoding="utf-8")
    second = archive_fallback_events(fb)
    assert second.archived
    assert second.archive_path is not None
    assert second.archive_path != first.archive_path, (
        "same-second second archive must NOT collide with first archive"
    )
    # First archive's bytes are unchanged (no clobber).
    assert first.archive_path.read_bytes() == first_bytes
    # Second archive's bytes have the new content.
    assert second.archive_path.read_text(encoding="utf-8") == "second\n"
