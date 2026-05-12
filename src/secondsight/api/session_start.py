"""POST /hook/session-start — convention injection endpoint (GUR-105, P3A-3).

This endpoint is called by the Claude Code SessionStart hook script. It
returns formatted conventions for system prompt injection. The response
is synchronous: the hook script blocks on it because Claude Code reads
stdout from the hook process as system prompt content.

Distinct from ``POST /hook/{agent}/{event_type}`` (hooks.py):
    That route handles event INGESTION (fire-and-forget, async background).
    This route handles convention INJECTION (synchronous, must return fast).
    They coexist on different URL paths without conflict.

Latency contract:
    Target < 50ms. The path is: DB indexed query (get_active_conventions)
    → in-memory budget selection → string formatting. No LLM calls, no
    network hops beyond the SQLite read. If a project has never been
    analysed (no conventions exist), the response is immediate with an
    empty conventions list.

Request body:
    {project_id: str, agent: str}
    - project_id: identifies which project's conventions to inject.
    - agent: identifies the adapter for formatting (e.g. "claude_code").

Response shape:
    {conventions: str, count: int, budget_used: int, budget_total: int}
    - conventions: the formatted multi-line string for system prompt injection.
      Empty string when no conventions are available.
    - count: number of conventions selected (for observability).
    - budget_used: estimated tokens consumed by the selected conventions.
    - budget_total: the configured token budget (default 2000).

Silent failure conditions:
    - If the project has no DB yet (never ingested), DirectivesRepository
      creation + create_schema() is called defensively (same pattern as
      api/directives.py). First session-start for a new project pays a
      one-time schema-creation cost (~5ms).
    - If the adapter does not implement inject_convention(), the endpoint
      returns 501 (Not Implemented). This makes it obvious that a new
      adapter type needs the override rather than silently injecting nothing.
    - If all conventions exceed the token budget individually, count=0 and
      conventions="" — correct behavior, not an error.

Design assumptions:
    - Single adapter per agent name. The AdapterRegistry.for_() dispatch
      uses a synthetic event_type ("session_start") to find the adapter.
    - The endpoint does NOT perform event ingestion. The SessionStart event
      is ingested separately via the existing POST /hook/session_start route.
      This endpoint is purely for the convention injection response.

Ref: SD §3.9, §6.3
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from secondsight.adapters.base import NoAdapterError
from secondsight.api._id_safety import is_safe_id
from secondsight.api.server import AppState
from secondsight.event import EventType
from secondsight.feedback.convention import ConventionSelector, _estimate_tokens
from secondsight.storage.directives_repository import DirectivesRepository

router = APIRouter()

_STRICT = ConfigDict(frozen=True, extra="forbid")


class SessionStartRequest(BaseModel):
    """Request body for convention injection at session start."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=1, max_length=128)
    agent: str = Field(..., min_length=1, max_length=64)


class SessionStartResponse(BaseModel):
    """Response from convention injection endpoint."""

    model_config = _STRICT

    conventions: str
    count: int
    budget_used: int
    budget_total: int


@router.post(
    "/hook/session-start",
    response_model=SessionStartResponse,
    summary="Query conventions for system prompt injection at session start",
    description=(
        "Called by the agent's SessionStart hook script. Returns formatted "
        "conventions within the token budget for system prompt injection. "
        "The response is synchronous — the hook script blocks on it."
    ),
)
async def session_start_conventions(
    body: SessionStartRequest,
    request: Request,
) -> SessionStartResponse:
    """Return formatted conventions for injection into the agent's system prompt."""
    if not is_safe_id(body.project_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"project_id {body.project_id!r} contains unsafe characters. "
                "Use alphanumeric, hyphen, underscore, colon, or dot."
            ),
        )
    if not is_safe_id(body.agent):
        raise HTTPException(
            status_code=422,
            detail="agent contains unsafe characters.",
        )

    state: AppState = request.app.state.server_state

    try:
        adapter = state.adapter_registry.for_(body.agent, EventType.SESSION_START.value)
    except NoAdapterError:
        raise HTTPException(
            status_code=422,
            detail="No adapter registered for the specified agent.",
        )

    try:
        resources = await state.registry.get(body.project_id)
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Project resources temporarily unavailable.",
        )

    repo = DirectivesRepository(resources.db_engine)
    repo.create_schema()

    selector = ConventionSelector(repo)
    conventions = selector.select(body.project_id)

    if not conventions:
        return SessionStartResponse(
            conventions="",
            count=0,
            budget_used=0,
            budget_total=selector.token_budget,
        )

    lines: list[str] = []
    for conv in conventions:
        try:
            formatted = adapter.inject_convention(conv)
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=501,
                detail=(
                    f"Adapter for agent={body.agent!r} does not implement inject_convention: {exc}"
                ),
            ) from exc
        if formatted:
            lines.append(formatted)

    conventions_text = "\n".join(lines)
    budget_used = sum(_estimate_tokens(c.instruction) for c in conventions)

    return SessionStartResponse(
        conventions=conventions_text,
        count=len(conventions),
        budget_used=budget_used,
        budget_total=selector.token_budget,
    )


__all__ = ["router"]
