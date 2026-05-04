"""SyncLog — JSONL append-only log of DB INSERT failures.

Consumed by P1-13 backfill: when the DB comes back, backfill iterates
the log, inserts each missing row from its raw_trace_path, and removes
the entry on success.

Atomicity model:
    Each call to record_failure performs a single os.write() of the
    line bytes. POSIX guarantees writes <= PIPE_BUF (4096 on most
    platforms) are atomic w.r.t. concurrent writers on the same fd.
    Our lines are ~300-500 bytes, well under the limit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class SyncLogEntry:
    event_id: str
    raw_trace_path: str
    error_class: str
    error_message: str
    timestamp: str


class SyncLog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record_failure(
        self,
        event_id: str,
        raw_trace_path: Path,
        error: BaseException,
    ) -> None:
        """Append one JSONL line. Single os.write call for atomicity."""
        entry = {
            "event_id": event_id,
            "raw_trace_path": str(raw_trace_path),
            "error_class": type(error).__name__,
            "error_message": str(error),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        # O_APPEND ensures concurrent writers each see the file's current end
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def iter_pending(self) -> Iterator[SyncLogEntry]:
        """Yield every recorded failure in append order.

        Truncated/garbage trailing lines are skipped silently — they
        represent a process killed mid-write and the next backfill run
        will see only complete lines anyway.
        """
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Truncated / corrupt — skip rather than poison the iterator.
                    continue
                yield SyncLogEntry(
                    event_id=obj["event_id"],
                    raw_trace_path=obj["raw_trace_path"],
                    error_class=obj["error_class"],
                    error_message=obj["error_message"],
                    timestamp=obj["timestamp"],
                )
