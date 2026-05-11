"""Directives API — `GET /api/directives`, `PATCH /api/directives/{id}`
(GUR-104 task-2).

Convention reuse from ``api/observation.py``:
- frozen Pydantic with ``extra="forbid"`` on every response shape
- required ``project_id: str = Query(...)`` per DC-4
- weak ETag derived from ``MAX(updated_at)`` over the project scope
- ``is_safe_id`` validation on path params

Schema-as-contract (D1): ``DirectiveOut`` and ``DirectivePatchRequest`` are
re-exported from this module and imported by ``cli/directive.py`` so the
CLI's ``--format json`` output is byte-identical to the API. Renaming or
removing a field here breaks the agent-self-query path; see
``problem-autopsy.md`` damage_recipients.

Idempotency (DC-2): PATCH with a payload whose target state matches the
current state returns 200 with the existing row but issues NO UPDATE
statement and does NOT advance ``updated_at``. The read+compare happens
in the same connection as the (possible) write so concurrent PATCHes
don't race.

Phase 3 cache caveat (DG-2.1): the OpenAPI route description for PATCH
explicitly notes that runtime cache invalidation lands in GUR-105 and
that, until then, a server restart may be required for a soft-disable
to stop firing in prompt injection.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
)
from secondsight.api._id_safety import is_safe_id
from secondsight.api.registry import ProjectResources
from secondsight.api.server import AppState  # type-only after lifespan setup
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.directives_table import directives as directives_table

_STRICT = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Pydantic shapes — also imported by cli/directive.py (schema-as-contract)
# ---------------------------------------------------------------------------


class DirectiveOut(BaseModel):
    """Response shape for a directive on the API and the CLI.

    Field set is the full ``Directive`` row from
    ``analysis/schemas.py:142`` — adding fields is OK, renaming or
    removing is breaking. The ``disabled_at`` and ``disabled_reason``
    fields are nullable but ALWAYS emitted (never omitted) so the
    response shape is byte-stable across active and disabled directives.
    See ``2-pre-thinking.md`` U2 lock.
    """

    model_config = _STRICT

    id: str
    project_id: str
    type: str
    status: str
    instruction: str
    frequency: float | None = None
    trigger_pattern: str | None = None
    confidence: float | None = None
    max_firing: int | None = None
    source_flag_type: str | None = None
    source_sessions: list[str] = Field(default_factory=list)
    identity_key: str
    created_at: datetime
    expires_at: datetime | None = None
    updated_at: datetime
    disabled_at: datetime | None = None
    disabled_reason: str | None = None

    @classmethod
    def from_directive(cls, directive: Directive) -> "DirectiveOut":
        return cls(
            id=directive.id,
            project_id=directive.project_id,
            type=directive.type.value,
            status=directive.status.value,
            instruction=directive.instruction,
            frequency=directive.frequency,
            trigger_pattern=directive.trigger_pattern,
            confidence=directive.confidence,
            max_firing=directive.max_firing,
            source_flag_type=directive.source_flag_type,
            source_sessions=list(directive.source_sessions),
            identity_key=directive.identity_key,
            created_at=directive.created_at,
            expires_at=directive.expires_at,
            updated_at=directive.updated_at,
            disabled_at=directive.disabled_at,
            disabled_reason=directive.disabled_reason,
        )


class DirectivePatchRequest(BaseModel):
    """Body for ``PATCH /api/directives/{id}``.

    User-PATCHable status values are constrained to ``{active, disabled}``
    per ``DirectivesRepository`` lifecycle contract. The other three
    statuses (``expired``, ``superseded``, ``obsolete``) are analyzer-set
    and rejected at the Pydantic layer with 422.

    Lifecycle rules enforced by ``model_validator``:
    - ``status == "disabled"`` requires non-empty ``reason``.
    - ``status == "active"`` requires ``reason`` to be absent (or None).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"]
    reason: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _reason_matches_status(self) -> "DirectivePatchRequest":
        if self.status == "disabled":
            if not self.reason:
                raise ValueError(
                    "PATCH directive: status=disabled requires a non-empty "
                    "reason (lifecycle contract). To re-enable, send "
                    'status="active" without reason.'
                )
        else:
            if self.reason is not None:
                raise ValueError(
                    "PATCH directive: status=active must NOT include reason. "
                    'To soft-disable, send status="disabled" with reason="…".'
                )
        return self


# ---------------------------------------------------------------------------
# ETag computation
# ---------------------------------------------------------------------------


def _hash_etag(seed: str) -> str:
    """Stable, short ETag value — quoted per RFC 7232. Mirrors
    ``api/observation.py``'s scheme so dashboards see the same shape.
    """
    digest = hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f'W/"{digest[:16]}"'


def _directives_etag(
    db_engine, project_id: str, *, active_only: bool
) -> str | None:
    """ETag for the directives listing.

    Scope: rows in ``directives`` where ``project_id`` matches and
    (if ``active_only``) ``status='active'``. The ETag also incorporates
    the row count so adding/removing a directive without changing
    ``updated_at`` (impossible in practice but defensive) still
    invalidates the cache.

    Returns ``None`` when the project has no in-scope directives.
    """
    where = [directives_table.c.project_id == project_id]
    if active_only:
        where.append(directives_table.c.status == DirectiveStatus.ACTIVE.value)

    stmt = sa.select(
        sa.func.max(directives_table.c.updated_at),
        sa.func.count(),
    ).where(*where)

    with db_engine.engine.connect() as conn:
        row = conn.execute(stmt).first()

    if row is None or row[1] == 0:
        return None
    max_updated = row[0]
    count = int(row[1])
    seed = f"{project_id}|active={active_only}|{max_updated.isoformat()}|{count}"
    return _hash_etag(seed)


# ---------------------------------------------------------------------------
# Repo factory — defensive create_schema for fresh projects
# ---------------------------------------------------------------------------


def _directives_repo(resources: ProjectResources) -> DirectivesRepository:
    """Build a ``DirectivesRepository`` for the project's DB engine.

    Calls ``create_schema()`` defensively: a fresh project (Phase 1
    ingest happened but Phase 2 analysis never ran) has no directives
    table; without this call, the first GET would 500 with
    ``OperationalError: no such table``. ``create_all(checkfirst=True)``
    is idempotent and cheap.
    """
    repo = DirectivesRepository(resources.db_engine)
    repo.create_schema()
    return repo


async def _aresources(request: Request, project_id: str) -> ProjectResources:
    """Project-scoped resource resolution with safe-id check.

    Mirrors ``api/observation._aresources`` — see that module for the
    rationale on why ``is_safe_id`` is enforced HERE (defence in depth
    against a future caller that bypasses ``Query(min_length=1)``).
    """
    if not is_safe_id(project_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"project_id {project_id!r} contains unsafe characters. "
                "Use alphanumeric, hyphen, underscore, colon, or dot."
            ),
        )
    state: AppState = request.app.state.server_state
    return await state.registry.get(project_id)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter()


@router.get(
    "/api/directives",
    response_model=None,
    summary="List directives for a project",
    description=(
        "Returns directives for the project. By default returns active "
        "directives only (``active=true``); pass ``active=false`` to "
        "include disabled directives. Listing carries an ETag derived "
        "from MAX(updated_at) + row count over the in-scope set; clients "
        "that echo it via ``If-None-Match`` get 304 with no body."
    ),
)
async def list_directives(
    request: Request,
    response: Response,
    project_id: str = Query(..., min_length=1, max_length=128),
    active: bool = Query(
        True,
        description=(
            "When true (default), return only directives with status "
            "'active'. When false, include disabled directives too. "
            "Other statuses (expired/superseded/obsolete) are analyzer-only "
            "and never surfaced via this endpoint."
        ),
    ),
) -> list[DirectiveOut] | Response:
    """List directives for a project. Active filter on by default (DC-5)."""
    resources = await _aresources(request, project_id)
    repo = _directives_repo(resources)

    etag = _directives_etag(
        resources.db_engine, project_id, active_only=active
    )
    if etag is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    directives_list = repo.list_for_project(
        project_id, active_only=active
    )

    if etag is not None:
        response.headers["ETag"] = etag
    return [DirectiveOut.from_directive(d) for d in directives_list]


@router.patch(
    "/api/directives/{directive_id}",
    response_model=None,
    summary="Soft-disable or re-activate a directive",
    description=(
        "Bidirectional active ↔ disabled transitions. PATCH with the "
        "current state is a no-op: returns 200 with the row but does NOT "
        "advance updated_at and does NOT issue a DB write (DC-2). "
        "PATCH status=disabled requires a non-empty reason; status=active "
        "must NOT include reason.\n\n"
        "**Phase 3 caveat (GUR-105):** PATCH writes the DB row and is "
        "immediately visible to subsequent GET requests. Runtime cache "
        "invalidation for prompt injection lands in GUR-105; until then, "
        "a server restart may be required for a soft-disable to stop "
        "firing in agent prompt injection."
    ),
)
async def patch_directive(
    request: Request,
    directive_id: str,
    body: DirectivePatchRequest,
    project_id: str = Query(..., min_length=1, max_length=128),
) -> DirectiveOut:
    """Soft-disable / re-activate a directive (DC-1, DC-2, lifecycle)."""
    if not is_safe_id(directive_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"directive_id {directive_id!r} contains unsafe characters."
            ),
        )

    resources = await _aresources(request, project_id)
    repo = _directives_repo(resources)

    requested_status = DirectiveStatus(body.status)

    try:
        result, _was_noop = repo.compare_and_update_status(
            directive_id, project_id, requested_status, body.reason
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "lifecycle_violation", "message": "Directive lifecycle rule violated."},
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"directive {directive_id!r} not found in project "
                f"{project_id!r}."
            ),
        )

    return DirectiveOut.from_directive(result)


__all__ = [
    "DirectiveOut",
    "DirectivePatchRequest",
    "router",
]
