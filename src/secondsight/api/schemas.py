"""Pydantic request envelopes for the SecondSight hook API (P1-5, Task-3).

Design assumptions:
- `extra="allow"` on HookEnvelope lets adapter-specific fields pass through
  into `payload`-level enrichment without breaking validation. Real adapters
  (P1-9..P1-11) may add fields; the envelope must not reject them.
- All fields have explicit validation constraints (min_length, max_length, ge)
  so Pydantic returns 422 with field-level errors before any production code runs.
- `timestamp` is adapter-supplied and must be timezone-aware; production adapters
  always send UTC. If a naive datetime arrives, the adapter layer is responsible
  for coercing it; the schema accepts both naive and aware datetimes for flexibility.
- `agent` is the canonical identifier for which adapter to select. It takes
  precedence over any X-SecondSight-Agent header; body wins.

Silent failure conditions:
- If `extra="allow"` allows an attacker to inject very large additional fields,
  we rely on uvicorn's body-size limit (default 1 MiB) to bound the attack surface.
  No per-field size limit is enforced on extra fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IngressEnvelope(BaseModel):
    """Minimum ingress envelope for agent-native hook payloads.

    The thin ingress contract preserves the raw payload and only requires the
    transport-owned metadata needed before adapter normalization.

    `session_id` / `project_id` remain optional here for compatibility with
    legacy callers and test adapters; the new `/hook/{agent}/{event_type}`
    path does not require them.
    """

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    sequence_number: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_id: str | None = Field(default=None, min_length=1, max_length=128)


class HookEnvelope(IngressEnvelope):
    """Legacy fully-formed envelope for backward compatibility.

    Adapter-specific fields are allowed (extra="allow") and flow into the
    adapter's `normalize()` call. Core fields are strictly validated.
    """

    model_config = ConfigDict(extra="allow")

    project_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    agent: str = Field(min_length=1, max_length=64)


__all__ = ["IngressEnvelope", "HookEnvelope"]
