"""Raw ingress record persisted alongside normalized Event artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


INGRESS_SCHEMA_VERSION = "1.0.0"


class IngressRecord(BaseModel):
    """Durable raw ingress shape for replay and adapter debugging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    sequence_number: int = Field(ge=0)
    session_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = INGRESS_SCHEMA_VERSION


__all__ = ["INGRESS_SCHEMA_VERSION", "IngressRecord"]
