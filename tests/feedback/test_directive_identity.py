"""Tests for project-scoped directive identity resolution."""

from __future__ import annotations

from datetime import datetime, timezone

from secondsight.analysis.schemas import (
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.feedback.directive_identity import resolve_or_create_identity


def _now() -> datetime:
    return datetime(2026, 5, 22, 16, 0, 0, tzinfo=timezone.utc)


def _directive(
    instruction: str,
    *,
    directive_id: str = "dir-1",
    identity_key: str = "lineage-1",
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
    source_flag_type: str = BehaviorFlagType.UNNECESSARY_READ.value,
) -> Directive:
    return Directive(
        id=directive_id,
        project_id="proj-1",
        type=DirectiveType.CONVENTION,
        status=status,
        instruction=instruction,
        identity_key=identity_key,
        source_flag_type=source_flag_type,
        created_at=_now(),
        updated_at=_now(),
    )


class TestResolveOrCreateIdentity:
    def test_same_concept_reuses_existing_identity(self) -> None:
        existing = [
            _directive(
                "Always use rg for repo search",
                directive_id="dir-existing",
                identity_key="lineage-existing",
            )
        ]

        result = resolve_or_create_identity(
            project_id="proj-1",
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            pattern_description="search tool misuse pattern",
            candidate_instruction="Always use rg for searching the repository",
            existing_directives=existing,
        )

        assert result.identity_key == "lineage-existing"
        assert result.matched_directive_id == "dir-existing"
        assert result.is_new_identity is False

    def test_obsolete_match_reuses_lineage_id(self) -> None:
        existing = [
            _directive(
                "Read only the target file instead of wandering",
                directive_id="dir-obsolete",
                identity_key="lineage-obsolete",
                status=DirectiveStatus.OBSOLETE,
            )
        ]

        result = resolve_or_create_identity(
            project_id="proj-1",
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            pattern_description="unnecessary read pattern",
            candidate_instruction="Read only the target file instead of wandering through the repo",
            existing_directives=existing,
        )

        assert result.identity_key == "lineage-obsolete"
        assert result.matched_directive_id == "dir-obsolete"
        assert result.matched_status is DirectiveStatus.OBSOLETE

    def test_new_concept_mints_new_identity(self) -> None:
        existing = [
            _directive(
                "Always use rg for repo search",
                directive_id="dir-existing",
                identity_key="lineage-existing",
            )
        ]

        result = resolve_or_create_identity(
            project_id="proj-1",
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            pattern_description="file selection pattern",
            candidate_instruction="Open the exact file path directly when it is already provided",
            existing_directives=existing,
        )

        assert result.is_new_identity is True
        assert result.matched_directive_id is None
        assert result.identity_key != "lineage-existing"

    def test_ambiguous_semantic_match_returns_unknown(self) -> None:
        existing = [
            _directive(
                "Open the exact target file directly",
                directive_id="dir-a",
                identity_key="lineage-a",
                status=DirectiveStatus.OBSOLETE,
            ),
            _directive(
                "Open the exact target file directly",
                directive_id="dir-b",
                identity_key="lineage-b",
                status=DirectiveStatus.STALLED,
            ),
        ]

        result = resolve_or_create_identity(
            project_id="proj-1",
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            pattern_description="exact file selection pattern",
            candidate_instruction="Open the exact target file directly",
            existing_directives=existing,
        )

        assert result.is_unknown is True
        assert result.is_new_identity is False
        assert result.identity_key is None
        assert result.match_reason.startswith("ambiguous_semantic_match:")
