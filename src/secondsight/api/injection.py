"""Dedicated hook injection endpoints.

These routes return raw hook stdout payloads. They are separate from ingest:
observation transport remains under ``/hook/{...}``, while injection owns
synchronous hook output semantics.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from secondsight.adapters.base import AgentAdapter, NoAdapterError
from secondsight.api._id_safety import is_safe_id
from secondsight.api.ingress import project_id_from_cwd
from secondsight.api.server import AppState
from secondsight.config.loader import load_project_config
from secondsight.config.schema import FeedbackConfig, SecondSightConfigError
from secondsight.event import EventType
from secondsight.feedback.convention import ConventionSelector
from secondsight.storage.directives_repository import DirectivesRepository

router = APIRouter()


class SessionStartInjectionRequest(BaseModel):
    """Request body for SessionStart injection."""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)


_SESSION_START_CONVENTION_HEADER = (
    "SecondSight project conventions:\n"
    "These are project-derived behavioral guidelines for this session. "
    "Follow them unless the user explicitly gives conflicting instructions."
)


def _render_session_start_convention_template(lines: list[str]) -> str | None:
    """Wrap selected convention lines in the SessionStart convention template."""
    non_empty_lines = [line.strip() for line in lines if line.strip()]
    if not non_empty_lines:
        return None
    return f"{_SESSION_START_CONVENTION_HEADER}\n\n" + "\n".join(non_empty_lines)


async def _build_session_start_text(
    *,
    project_id: str,
    feedback_config: FeedbackConfig,
    repo: DirectivesRepository,
    adapter: AgentAdapter,
) -> str | None:
    """Select conventions with resolved budget and assemble SessionStart text."""
    selector = ConventionSelector(
        repo,
        token_budget=feedback_config.convention_injection_budget,
    )
    conventions = selector.select(project_id)
    lines = [adapter.inject_convention(convention) for convention in conventions]
    return _render_session_start_convention_template(lines)


def _validate_path_id(name: str, value: str) -> None:
    if not is_safe_id(value):
        raise HTTPException(status_code=422, detail=f"{name} contains unsafe characters.")


def _validated_hook_cwd(cwd: str) -> Path:
    """Validate hook-provided cwd and return it as an absolute Path."""
    if "\x00" in cwd:
        raise HTTPException(status_code=422, detail="cwd contains unsafe characters.")
    path = Path(cwd)
    if not path.is_absolute():
        raise HTTPException(status_code=422, detail="cwd must be an absolute path.")
    return path


def _project_id_from_hook_context(project_id: str | None, cwd: str | None) -> str:
    """Resolve injection project_id with cwd as the canonical hook source."""
    if cwd is not None:
        try:
            resolved = project_id_from_cwd(str(_validated_hook_cwd(cwd)))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="Cannot derive project_id from cwd.",
            ) from exc
        _validate_path_id("project_id", resolved)
        return resolved
    if project_id is None:
        raise HTTPException(status_code=422, detail="project_id or cwd is required.")
    _validate_path_id("project_id", project_id)
    return project_id


@router.post("/hook/injection/session-start/{agent}")
async def session_start_injection(
    agent: str,
    body: SessionStartInjectionRequest,
    request: Request,
) -> Response:
    """Return raw SessionStart hook payload or 204 when there is nothing to inject."""
    _validate_path_id("agent", agent)
    project_id = _project_id_from_hook_context(body.project_id, body.cwd)

    state: AppState = request.app.state.server_state
    try:
        adapter = state.adapter_registry.for_(agent, EventType.SESSION_START.value)
    except NoAdapterError:
        raise HTTPException(
            status_code=422, detail="No adapter registered for the specified agent."
        )

    try:
        cfg = load_project_config(state.secondsight_home, project_id)
        resources = await state.registry.get(project_id)
        repo = DirectivesRepository(resources.db_engine)
        repo.create_schema()
        text = await _build_session_start_text(
            project_id=project_id,
            feedback_config=cfg.feedback,
            repo=repo,
            adapter=adapter,
        )
    except SecondSightConfigError as exc:
        logger.error(
            "SessionStart injection config resolution failed: project_id={pid} error={err}",
            pid=project_id,
            err=exc,
        )
        raise HTTPException(status_code=500, detail="Injection config resolution failed.") from exc
    except RuntimeError as exc:
        logger.error(
            "SessionStart injection resources unavailable: project_id={pid} error={err}",
            pid=project_id,
            err=exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Project resources temporarily unavailable.",
        ) from exc
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=f"Adapter for agent={agent!r} does not implement convention injection: {exc}",
        ) from exc

    if not text:
        return Response(status_code=204)

    try:
        payload = adapter.render_session_start_output(text)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "SessionStart injection render failed: agent={agent} project_id={pid} error={err}",
            agent=agent,
            pid=project_id,
            err=exc,
        )
        raise HTTPException(status_code=500, detail="Injection render failed.") from exc

    return Response(content=payload, media_type="application/json")


__all__ = [
    "SessionStartInjectionRequest",
    "_build_session_start_text",
    "_project_id_from_hook_context",
    "_render_session_start_convention_template",
    "router",
]
