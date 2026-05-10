"""Tests for convention semantic dedup (GUR-108, P3B-1)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from secondsight.analysis.schemas import Directive, DirectiveStatus, DirectiveType
from secondsight.feedback.dedup import (
    DEDUP_SIMILARITY_THRESHOLD,
    DedupResult,
    DedupVerdict,
    _jaccard_similarity,
    _normalize_tokens,
    check_semantic_dedup,
)


def _make_directive(
    instruction: str,
    *,
    directive_id: str = "d-1",
    identity_key: str = "key-1",
) -> Directive:
    now = datetime.now(tz=timezone.utc)
    return Directive(
        id=directive_id,
        project_id="proj-test",
        type=DirectiveType.CONVENTION,
        status=DirectiveStatus.ACTIVE,
        instruction=instruction,
        identity_key=identity_key,
        created_at=now,
        updated_at=now,
    )


class TestNormalizeTokens:
    def test_basic_normalization(self) -> None:
        tokens = _normalize_tokens("Always use grep for searching!")
        assert tokens == {"always", "use", "grep", "for", "searching"}

    def test_strips_punctuation(self) -> None:
        tokens = _normalize_tokens("don't use rm -rf /")
        assert "dont" in tokens
        assert "use" in tokens

    def test_empty_string(self) -> None:
        assert _normalize_tokens("") == set()


class TestJaccardSimilarity:
    def test_identical_sets(self) -> None:
        assert _jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self) -> None:
        assert _jaccard_similarity({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self) -> None:
        assert _jaccard_similarity({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_empty_sets(self) -> None:
        assert _jaccard_similarity(set(), set()) == 1.0

    def test_one_empty(self) -> None:
        assert _jaccard_similarity(set(), {"a"}) == 0.0


class TestCheckSemanticDedup:
    def test_truly_new_returns_add(self) -> None:
        existing = [_make_directive("Always use grep for searching")]
        result = check_semantic_dedup(
            "Prefer TypeScript over JavaScript for new files", existing,
        )
        assert result.verdict == DedupVerdict.ADD

    def test_exact_duplicate_returns_skip(self) -> None:
        existing = [_make_directive("Always use grep for searching")]
        result = check_semantic_dedup(
            "Always use grep for searching", existing,
        )
        assert result.verdict == DedupVerdict.SKIP
        assert result.similarity == 1.0

    def test_semantic_duplicate_returns_skip(self) -> None:
        existing = [_make_directive("Always use grep for searching files")]
        result = check_semantic_dedup(
            "Always use grep for searching", existing,
        )
        assert result.similarity >= DEDUP_SIMILARITY_THRESHOLD
        assert result.verdict == DedupVerdict.SKIP

    def test_more_precise_returns_supersede(self) -> None:
        existing = [_make_directive("Use grep for searching files")]
        result = check_semantic_dedup(
            "Always use grep for searching files carefully",
            existing,
        )
        assert result.verdict == DedupVerdict.SUPERSEDE
        assert result.matched_directive_id == "d-1"

    def test_empty_instruction_returns_skip(self) -> None:
        result = check_semantic_dedup("", [])
        assert result.verdict == DedupVerdict.SKIP

    def test_no_existing_conventions_returns_add(self) -> None:
        result = check_semantic_dedup("Always use grep", [])
        assert result.verdict == DedupVerdict.ADD

    def test_exclude_identity_key_skips_self(self) -> None:
        existing = [_make_directive("Always use grep for searching", identity_key="key-self")]
        result = check_semantic_dedup(
            "Always use grep for searching",
            existing,
            exclude_identity_key="key-self",
        )
        assert result.verdict == DedupVerdict.ADD

    def test_exclude_identity_key_does_not_skip_others(self) -> None:
        existing = [
            _make_directive("Always use grep for searching", identity_key="key-other"),
        ]
        result = check_semantic_dedup(
            "Always use grep for searching",
            existing,
            exclude_identity_key="key-self",
        )
        assert result.verdict == DedupVerdict.SKIP
