"""Revision candidate seam for directive rewrite strategy."""

from __future__ import annotations

from dataclasses import dataclass

from secondsight.analysis.schemas import BehaviorFlagType, Directive

_REVISION_HINTS: dict[str, str] = {
    BehaviorFlagType.UNNECESSARY_READ.value: (
        "Prefer the explicit target path over broad repository exploration."
    ),
    BehaviorFlagType.REDUNDANT_EXPLORATION.value: (
        "Stop exploring once the needed file or answer is already found."
    ),
    BehaviorFlagType.MISSED_SHORTCUT.value: (
        "Use the shortest reliable path when the target is already known."
    ),
    BehaviorFlagType.REPEATED_OPERATION.value: (
        "Do not repeat the same operation after a successful result."
    ),
    BehaviorFlagType.WRONG_TOOL_CHOICE.value: (
        "Choose the narrowest tool that directly answers the task."
    ),
    BehaviorFlagType.EXCESSIVE_CONTEXT_GATHERING.value: (
        "Limit context gathering to artifacts directly tied to the current task."
    ),
}


@dataclass(frozen=True, slots=True)
class RevisionCandidate:
    instruction: str
    strategy: str


def build_revision_candidate(directive: Directive) -> RevisionCandidate | None:
    """Return the current deterministic revision placeholder.

    This is intentionally a seam: a future rewrite feature can swap this for an
    LLM-backed builder/reviewer path without changing aggregator orchestration.
    """
    if not directive.source_flag_type:
        return None
    instruction = _REVISION_HINTS.get(directive.source_flag_type)
    if not instruction:
        return None
    return RevisionCandidate(
        instruction=instruction,
        strategy="deterministic_flag_type_placeholder",
    )


__all__ = [
    "RevisionCandidate",
    "build_revision_candidate",
]
