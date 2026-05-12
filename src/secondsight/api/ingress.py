"""Ingress helpers for hook transport metadata and adapter-derived IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_PROJECT_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class IngressContext:
    """Routing metadata known before adapter normalization."""

    agent: str
    event_type: str


def project_id_from_cwd(cwd: str) -> str:
    """Derive a filesystem-safe project_id from a cwd path.

    The current canonicalization is intentionally simple:
      1. take the last path component
      2. replace non-safe characters with '-'
      3. strip leading/trailing separators

    Empty results are rejected loudly so callers do not silently write to an
    unnamed project bucket.
    """

    name = Path(cwd).name
    slug = _PROJECT_ID_SANITIZE_RE.sub("-", name).strip(".-")
    if not slug:
        raise ValueError(f"Cannot derive project_id from cwd={cwd!r}")
    return slug


__all__ = ["IngressContext", "project_id_from_cwd"]
