"""Hook ingestion routes.

The handler performs exactly four steps in order:
  1. Validate the envelope (Pydantic; done by FastAPI before the handler runs).
     Additionally validate event_type against the closed EventType enum.
  2. Route the payload to the right AgentAdapter → PartialEvent.
  3. Hand the partial to SessionTracker.bind → fully-formed Event.
  4. Schedule pipeline.ingest(event) via asyncio.create_task; return {"status": "ok"}.

The latency contract is structural:
  - The handler does NOT await the ingest task. `asyncio.create_task` schedules it.
  - A `add_done_callback` on every task catches ingest exceptions and logs them
    structurally via loguru. Without this, asyncio silently drops exceptions on GC.
  - In-flight tasks are tracked in `app.state.server_state.inflight_tasks` (strong set)
    so the lifespan shutdown can drain them. A discard done_callback removes each task
    when it completes, preventing unbounded set growth.

Design assumptions:
  - `app.state.server_state` is an AppState (typed, set during lifespan startup).
  - `body.agent` is canonical; any X-SecondSight-Agent header is ignored.
  - project_id path component safety: the HookEnvelope enforces min_length=1 and
    max_length=128 but does not validate character set. The registry's
    _build_resources uses project_id as a directory name — path traversal characters
    (e.g. '/', '..') in project_id would be dangerous. We add a strict allowlist
    validation here before calling registry.get().

Silent failure conditions:
  - If add_done_callback raises inside the callback, Python ignores it entirely.
    The callback is wrapped in try/except to prevent this.
  - If asyncio.create_task is called outside a running event loop, it raises
    RuntimeError. This cannot happen inside an async handler, but if a future
    refactor moves the call to a sync context, it silently changes semantics.
  - If adapter.normalize raises ValueError (missing required fields), we
    return 422. Any other exception from normalize propagates as 500 — acceptable
    for Phase 1 (only IdentityAdapter is registered; real adapters must handle
    their own ValueError paths).

This module assumes:
  - Single asyncio event loop / single uvicorn worker (documented in registry.py).
  - The EventType enum is closed; unknown event_types always return 422.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from secondsight.adapters import NoAdapterError
from secondsight.api._id_safety import is_safe_id as _is_safe_id
from secondsight.api.schemas import HookEnvelope, IngressEnvelope
from secondsight.event import EventType
from secondsight.storage.ingress_record import IngressRecord

if TYPE_CHECKING:
    from secondsight.api.server import AppState

router = APIRouter()


def _task_done_callback(
    task: asyncio.Task[None],
    *,
    event_id: str,
) -> None:
    """Log any exception raised by the ingest task.

    This callback is called by the asyncio event loop when the task completes.
    It MUST NOT raise — any exception from a done_callback is silently swallowed
    by asyncio (the task result is already set; raising here loses the exception
    with no traceback). We wrap the entire body in try/except BaseException.

    We explicitly do NOT suppress asyncio.CancelledError as an error — a cancelled
    task is a normal shutdown outcome, not a data-loss event.
    """
    try:
        exc = task.exception()
        if exc is None:
            # Task completed normally.
            return

        # Ingest failed. Log structurally including the event_id for traceability.
        logger.error(
            "Ingest task failed for event_id={event_id}: {exc_type}: {exc}",
            event_id=event_id,
            exc_type=type(exc).__name__,
            exc=exc,
        )
    except asyncio.CancelledError:
        # task.exception() raises CancelledError when the task was cancelled.
        # This except MUST come before `except BaseException` because CancelledError
        # inherits from BaseException (not Exception) — catching BaseException first
        # would consume it and the cancellation would be misreported as a callback error.
        logger.info(
            "Ingest task cancelled (shutdown) for event_id={event_id}",
            event_id=event_id,
        )
    except BaseException as cb_err:  # noqa: BLE001
        # The callback itself exploded. We cannot raise (asyncio ignores it),
        # so write to stderr as a last resort.
        import sys

        print(
            f"CRITICAL: done_callback itself raised for event_id={event_id!r}: "
            f"{type(cb_err).__name__}: {cb_err}",
            file=sys.stderr,
        )


async def _handle_ingest(
    *,
    agent: str,
    event_type: str,
    envelope: IngressEnvelope,
    request: Request,
) -> dict[str, str]:
    """Accept a hook event and schedule fire-and-forget ingestion.

    Four-step contract (see module docstring):
      1. Validate event_type against the closed EventType enum → 422 if unknown.
      2. Validate project_id and session_id for path safety → 422 if unsafe.
      3. Resolve adapter → PartialEvent → tracker.bind() → Event.
      4. Schedule pipeline.ingest via create_task; return {"status": "ok"}.

    The response is returned BEFORE ingest completes. This is the latency
    contract. do NOT add `await` before the pipeline call.
    """
    # --- Step 1: Validate event_type against the closed enum ---
    try:
        validated_event_type = EventType(event_type)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event_type {event_type!r}. "
            f"Valid values: {[e.value for e in EventType]}",
        )

    state = cast("AppState", request.app.state.server_state)

    # --- Step 3a: Resolve adapter and produce PartialEvent ---
    try:
        adapter = state.adapter_registry.for_(agent, validated_event_type.value)
    except NoAdapterError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    # I8: wrap normalize() — the AgentAdapter ABC declares ValueError as the
    # failure mode for missing required fields. Without this catch, a ValueError
    # propagates as an unhandled 500. We surface it as 422 so the caller can
    # fix the payload.
    try:
        partial = adapter.normalize(envelope, validated_event_type.value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Adapter rejected envelope: {exc}",
        ) from exc

    # --- Step 3a.5: Validate adapter-derived IDs for path safety ---
    if not _is_safe_id(partial.project_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"project_id {partial.project_id!r} contains unsafe characters. "
                f"Use alphanumeric, hyphen, underscore, or dot."
            ),
        )
    if not _is_safe_id(partial.session_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"session_id {partial.session_id!r} contains unsafe characters. "
                f"Use alphanumeric, hyphen, underscore, or dot."
            ),
        )

    # --- Resolve project resources ---
    try:
        resources = await state.registry.get(partial.project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # --- Step 3b: SessionTracker.bind → fully-formed Event ---
    tracker = await state.get_or_create_tracker(partial.project_id)
    try:
        event = await tracker.bind(partial)
    except Exception as exc:
        # tracker.bind raises SubAgentStackMismatch, ValueError, or WarmStart errors.
        # These are correctness errors — return 422 so the caller can retry/debug.
        raise HTTPException(
            status_code=422,
            detail=f"Tracker bind failed: {type(exc).__name__}: {exc}",
        ) from exc

    # --- Step 4: Schedule ingest as a fire-and-forget task ---
    # CRITICAL: do NOT add `await` here. This is the latency contract.
    # The handler returns immediately; ingest runs in the background.
    ingress_record = IngressRecord(
        agent=agent,
        event_type=validated_event_type.value,
        event_id=envelope.event_id,
        timestamp=envelope.timestamp,
        sequence_number=envelope.sequence_number,
        session_id=partial.session_id,
        project_id=partial.project_id,
        payload=dict(envelope.payload),
    )
    task = asyncio.create_task(
        resources.pipeline.ingest(event, ingress_record=ingress_record)
    )

    # Attach done_callback for structured error logging.
    # Without this, asyncio swallows exceptions when the task is GC'd.
    task.add_done_callback(lambda t: _task_done_callback(t, event_id=event.id))

    # Track in-flight tasks so shutdown can drain them.
    # C1: use a strong-reference set (not WeakSet) so the drain can enumerate all
    # tasks that were in-flight at snapshot time. The discard callback removes each
    # task when it completes, preventing unbounded set growth.
    # Multiple done_callbacks on the same task are supported — both this discard
    # callback and the error-logging callback above will fire.
    state.inflight_tasks.add(task)
    task.add_done_callback(state.inflight_tasks.discard)

    return {"status": "ok"}


@router.post("/hook/{agent}/{event_type}")
async def handle_ingress_hook(
    agent: str,
    event_type: str,
    envelope: IngressEnvelope,
    request: Request,
) -> dict[str, str]:
    """Thin ingress route for agent-native payloads."""
    if not _is_safe_id(agent):
        raise HTTPException(status_code=422, detail="agent contains unsafe characters.")
    return await _handle_ingest(
        agent=agent,
        event_type=event_type,
        envelope=envelope,
        request=request,
    )


@router.post("/hook/{event_type}")
async def handle_hook(
    event_type: str,
    envelope: HookEnvelope,
    request: Request,
) -> dict[str, str]:
    """Legacy fully-formed envelope route retained for compatibility."""
    return await _handle_ingest(
        agent=envelope.agent,
        event_type=event_type,
        envelope=envelope,
        request=request,
    )


__all__ = ["router"]
