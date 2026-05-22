"""Directive lineage identity resolution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from secondsight.analysis.schemas import BehaviorFlagType, Directive, DirectiveStatus, DirectiveType
from secondsight.feedback.dedup import find_semantic_match

_IDENTITY_STATUSES = {
    DirectiveStatus.ACTIVE,
    DirectiveStatus.OBSOLETE,
    DirectiveStatus.STALLED,
}


@dataclass(frozen=True, slots=True)
class IdentityResolutionResult:
    identity_key: str | None
    matched_directive_id: str | None
    matched_status: DirectiveStatus | None
    is_new_identity: bool
    is_unknown: bool
    match_reason: str


def _new_identity_key() -> str:
    return f"lineage-{uuid.uuid4()}"


def resolve_or_create_identity(
    *,
    project_id: str,
    flag_type: BehaviorFlagType,
    pattern_description: str,
    candidate_instruction: str,
    representative_sessions: list[str] | None = None,
    existing_directives: list[Directive],
) -> IdentityResolutionResult:
    """Reuse an existing project-scoped lineage id or mint a new one."""
    del pattern_description  # v1 resolution is instruction-led; keep arg for future evolution.

    candidates = [
        directive
        for directive in existing_directives
        if directive.project_id == project_id
        and directive.type is DirectiveType.CONVENTION
        and directive.status in _IDENTITY_STATUSES
        and directive.source_flag_type == flag_type.value
    ]

    if representative_sessions is not None:
        candidate_session_set = set(representative_sessions)
        for directive in candidates:
            if set(directive.source_sessions) == candidate_session_set:
                return IdentityResolutionResult(
                    identity_key=directive.identity_key,
                    matched_directive_id=directive.id,
                    matched_status=directive.status,
                    is_new_identity=False,
                    is_unknown=False,
                    match_reason="source_sessions_reuse",
                )

    semantic_match = find_semantic_match(
        candidate_instruction,
        candidates,
    )
    if semantic_match is None:
        return IdentityResolutionResult(
            identity_key=_new_identity_key(),
            matched_directive_id=None,
            matched_status=None,
            is_new_identity=True,
            is_unknown=False,
            match_reason="new_lineage",
        )
    if semantic_match.ambiguous:
        return IdentityResolutionResult(
            identity_key=None,
            matched_directive_id=None,
            matched_status=None,
            is_new_identity=False,
            is_unknown=True,
            match_reason=("ambiguous_semantic_match:" + ",".join(semantic_match.candidate_ids)),
        )

    matched = next(d for d in candidates if d.id == semantic_match.directive_id)
    return IdentityResolutionResult(
        identity_key=matched.identity_key,
        matched_directive_id=matched.id,
        matched_status=matched.status,
        is_new_identity=False,
        is_unknown=False,
        match_reason="semantic_reuse",
    )
