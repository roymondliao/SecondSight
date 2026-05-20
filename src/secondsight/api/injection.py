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
from secondsight.api.server import AppState
from secondsight.config.loader import load_project_config
from secondsight.config.schema import FeedbackConfig, SecondSightConfigError
from secondsight.event import EventType
from secondsight.feedback.convention import ConventionSelector
from secondsight.feedback.prompt_evaluator import (
    PromptEvaluationDecision,
    evaluate_user_prompt,
)
from secondsight.feedback.prompt_guidance import (
    bypass_registry,
    guidance_for_category,
)
from secondsight.state import SecondSightState, SecondSightStateError
from secondsight.storage.directives_repository import DirectivesRepository

router = APIRouter()


class SessionStartInjectionRequest(BaseModel):
    """Request body for SessionStart injection."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=1, max_length=128)


class UserPromptInjectionRequest(BaseModel):
    """Request body for UserPromptSubmit injection."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=1, max_length=128)
    prompt: str = Field(..., min_length=1)
    session_id: str | None = None
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


def _project_root_from_cwd(cwd: str | None, *, fallback: Path) -> Path:
    """Validate hook-provided cwd and return evaluator project_root."""
    if cwd is None:
        return fallback
    if "\x00" in cwd:
        raise HTTPException(status_code=422, detail="cwd contains unsafe characters.")
    path = Path(cwd)
    if not path.is_absolute():
        raise HTTPException(status_code=422, detail="cwd must be an absolute path.")
    return path


@router.post("/hook/injection/session-start/{agent}")
async def session_start_injection(
    agent: str,
    body: SessionStartInjectionRequest,
    request: Request,
) -> Response:
    """Return raw SessionStart hook payload or 204 when there is nothing to inject."""
    _validate_path_id("agent", agent)
    _validate_path_id("project_id", body.project_id)

    state: AppState = request.app.state.server_state
    try:
        adapter = state.adapter_registry.for_(agent, EventType.SESSION_START.value)
    except NoAdapterError:
        raise HTTPException(
            status_code=422, detail="No adapter registered for the specified agent."
        )

    try:
        cfg = load_project_config(state.secondsight_home, body.project_id)
        resources = await state.registry.get(body.project_id)
        repo = DirectivesRepository(resources.db_engine)
        repo.create_schema()
        text = await _build_session_start_text(
            project_id=body.project_id,
            feedback_config=cfg.feedback,
            repo=repo,
            adapter=adapter,
        )
    except SecondSightConfigError as exc:
        logger.error(
            "SessionStart injection config resolution failed: project_id={pid} error={err}",
            pid=body.project_id,
            err=exc,
        )
        raise HTTPException(status_code=500, detail="Injection config resolution failed.") from exc
    except RuntimeError as exc:
        logger.error(
            "SessionStart injection resources unavailable: project_id={pid} error={err}",
            pid=body.project_id,
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
            pid=body.project_id,
            err=exc,
        )
        raise HTTPException(status_code=500, detail="Injection render failed.") from exc

    return Response(content=payload, media_type="application/json")


@router.post("/hook/injection/user-prompt/{agent}")
async def user_prompt_injection(
    agent: str,
    body: UserPromptInjectionRequest,
    request: Request,
) -> Response:
    """Return raw UserPromptSubmit guidance payload or 204 on pass/bypass/fail-open."""
    _validate_path_id("agent", agent)
    _validate_path_id("project_id", body.project_id)

    state: AppState = request.app.state.server_state
    try:
        adapter = state.adapter_registry.for_(agent, EventType.USER_PROMPT.value)
    except NoAdapterError:
        raise HTTPException(
            status_code=422, detail="No adapter registered for the specified agent."
        )

    if bypass_registry.should_bypass(agent=agent, prompt=body.prompt):
        return Response(status_code=204)

    try:
        cfg = load_project_config(state.secondsight_home, body.project_id)
    except SecondSightConfigError as exc:
        logger.warning(
            "UserPrompt injection config resolution failed open: project_id={pid} error={err}",
            pid=body.project_id,
            err=exc,
        )
        return Response(status_code=204)

    project_root = _project_root_from_cwd(body.cwd, fallback=state.secondsight_home)

    try:
        evaluation = await evaluate_user_prompt(
            prompt=body.prompt,
            mode_config=cfg.general,
            analysis_config=cfg.analysis,
            providers_config=cfg.providers,
            project_root=project_root,
            session_id=body.session_id,
            resolved_cli_agent=_resolve_cli_agent_from_state(
                secondsight_home=state.secondsight_home,
                configured_agent=cfg.analysis.cli.default_agent,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "UserPrompt evaluator raised and failed open: agent={agent} project_id={pid} "
            "session_id={sid} error={err}",
            agent=agent,
            pid=body.project_id,
            sid=body.session_id,
            err=exc,
        )
        return Response(status_code=204)

    if str(evaluation.decision) != PromptEvaluationDecision.INTERVENE.value:
        if getattr(evaluation, "failure_reason", None):
            logger.warning(
                "UserPrompt evaluator failed open: agent={agent} project_id={pid} "
                "session_id={sid} reason={reason}",
                agent=agent,
                pid=body.project_id,
                sid=body.session_id,
                reason=evaluation.failure_reason,
            )
        return Response(status_code=204)
    if evaluation.primary_category is None:
        logger.warning(
            "UserPrompt evaluator returned intervene without category; failing open: "
            "agent={agent} project_id={pid} session_id={sid}",
            agent=agent,
            pid=body.project_id,
            sid=body.session_id,
        )
        return Response(status_code=204)

    try:
        guidance = guidance_for_category(evaluation.primary_category)
        payload = adapter.render_user_prompt_output(guidance)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "UserPrompt injection render failed: agent={agent} project_id={pid} error={err}",
            agent=agent,
            pid=body.project_id,
            err=exc,
        )
        raise HTTPException(status_code=500, detail="Injection render failed.") from exc

    return Response(content=payload, media_type="application/json")


def _resolve_cli_agent_from_state(*, secondsight_home: Path, configured_agent: str) -> str | None:
    """Resolve CLI default_agent='auto' for classifier subprocess routing."""
    if configured_agent != "auto":
        return configured_agent
    try:
        state = SecondSightState.load(secondsight_home / "state.json")
    except SecondSightStateError as exc:
        logger.warning("UserPrompt evaluator state resolution failed open: error={err}", err=exc)
        return None
    if state is None:
        return None
    return state.init_agent


__all__ = [
    "SessionStartInjectionRequest",
    "UserPromptInjectionRequest",
    "_build_session_start_text",
    "_project_root_from_cwd",
    "_render_session_start_convention_template",
    "_resolve_cli_agent_from_state",
    "router",
]
