"""Filesystem-first durability for raw ingress records."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from secondsight.storage.ingress_record import IngressRecord
from secondsight.storage.raw_trace_store import (
    RawTraceCorruptionError,
    UnsafePathError,
    is_safe_session_id,
)


class RawIngressStore:
    """Filesystem store for raw ingress records. One JSON file per record."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root).resolve()

    def ingress_path(self, record: IngressRecord) -> Path:
        if not is_safe_session_id(record.session_id):
            raise UnsafePathError(f"session_id contains unsafe characters: {record.session_id!r}")
        ingress_dir = self._project_root / "sessions" / record.session_id / "ingress"
        ts_part = (
            record.timestamp.strftime("%Y%m%dT%H%M%S")
            + f"{record.timestamp.microsecond // 1000:03d}Z"
        )
        filename = f"{ts_part}_{record.event_type}_seq{record.sequence_number:06d}.json"
        return ingress_dir / filename

    async def write(self, record: IngressRecord) -> Path:
        target = self.ingress_path(record)
        return await asyncio.to_thread(self._write_sync, record, target)

    @staticmethod
    def _write_sync(record: IngressRecord, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json().encode("utf-8")
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_",
            suffix=".json",
            dir=str(target.parent),
        )
        needs_cleanup = True
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
            needs_cleanup = False
        finally:
            if needs_cleanup and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return target

    async def read(self, path: Path) -> IngressRecord:
        return await asyncio.to_thread(self._read_sync, path)

    @staticmethod
    def _read_sync(path: Path) -> IngressRecord:
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise RawTraceCorruptionError(f"missing file: {path}") from exc
        except OSError as exc:
            raise RawTraceCorruptionError(f"cannot read {path}: {exc}") from exc
        if not raw:
            raise RawTraceCorruptionError(f"empty file: {path}")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RawTraceCorruptionError(f"invalid JSON in {path}: {exc}") from exc
        try:
            return IngressRecord.model_validate(obj)
        except Exception as exc:
            raise RawTraceCorruptionError(
                f"ingress record validation failed for {path}: {exc}"
            ) from exc


__all__ = ["RawIngressStore"]
