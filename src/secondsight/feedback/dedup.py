"""Convention semantic dedup (GUR-108, P3B-1).

Before adding a new convention, compares its instruction text against
existing active conventions for the same project. Three outcomes:

    1. **Semantic duplicate** — new instruction is highly similar to an
       existing active convention → skip (do not add).
    2. **More precise overlap** — new instruction covers the same topic
       but is more specific → supersede the existing convention.
    3. **Truly new** — no significant overlap → add normally.

Similarity metric:
    Jaccard similarity on normalized word tokens. Conventions are short
    imperative sentences (typically 10-40 words); Jaccard on words is
    sufficient and avoids embedding model infrastructure.

    Threshold: 0.7 — empirically chosen for instruction-length text.
    Two conventions sharing 70%+ of their vocabulary after normalization
    are considered semantically overlapping.

Supersede heuristic:
    When overlap exceeds the threshold, the LONGER instruction is
    considered more precise (it adds detail to the same topic). The
    shorter one is superseded. If lengths are equal, the newer one wins
    (recency bias — the newer aggregation has more data).

Silent failure conditions:
    - If all existing conventions have empty instructions, no dedup
      occurs (no overlap possible). Correct behavior.
    - If the new convention's instruction is empty, it is skipped
      entirely (Convention.instruction="" produces no injection output).

Design assumptions:
    - Dedup is per-project, not cross-project.
    - Dedup compares against ACTIVE conventions only. Disabled/expired/
      obsolete conventions are not considered (they are not injected).
    - The dedup check runs synchronously in the aggregator's Step 3
      loop, before the UPSERT call.

Ref: SD §5.9.2
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from secondsight.analysis.schemas import Directive

_logger = logging.getLogger(__name__)

DEDUP_SIMILARITY_THRESHOLD = 0.66
_DEFAULT_IDENTITY_THRESHOLD = 0.5
_STOPWORDS = {
    "a",
    "an",
    "and",
    "already",
    "for",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "when",
}
_TOKEN_EQUIVALENTS = {
    "repository": "repo",
    "repos": "repo",
    "searching": "search",
    "searched": "search",
    "searches": "search",
    "files": "file",
    "paths": "path",
}


class DedupVerdict(str, Enum):
    ADD = "add"
    SKIP = "skip"
    SUPERSEDE = "supersede"


@dataclass(frozen=True, slots=True)
class DedupResult:
    verdict: DedupVerdict
    matched_directive_id: str | None = None
    similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    directive_id: str | None
    identity_key: str | None
    similarity: float
    ambiguous: bool = False
    candidate_ids: tuple[str, ...] = ()


def _normalize_tokens(text: str) -> set[str]:
    """Lowercase, strip punctuation, split into word tokens."""
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    normalized: set[str] = set()
    for raw in cleaned.split():
        token = _TOKEN_EQUIVALENTS.get(raw, raw)
        if token in _STOPWORDS:
            continue
        normalized.add(token)
    return normalized


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _best_semantic_match(
    new_instruction: str,
    existing_conventions: list["Directive"],
    *,
    exclude_identity_key: str | None = None,
    threshold: float = DEDUP_SIMILARITY_THRESHOLD,
) -> tuple["Directive", float] | None:
    if not new_instruction:
        return None

    new_tokens = _normalize_tokens(new_instruction)
    if not new_tokens:
        return None

    best_similarity = 0.0
    best_match: Directive | None = None

    for existing in existing_conventions:
        if exclude_identity_key and existing.identity_key == exclude_identity_key:
            continue
        if not existing.instruction:
            continue
        existing_tokens = _normalize_tokens(existing.instruction)
        if not existing_tokens:
            continue

        sim = _jaccard_similarity(new_tokens, existing_tokens)
        if sim > best_similarity:
            best_similarity = sim
            best_match = existing

    if best_match is None or best_similarity < threshold:
        return None
    return best_match, best_similarity


def find_semantic_match(
    new_instruction: str,
    existing_conventions: list["Directive"],
    *,
    exclude_identity_key: str | None = None,
    threshold: float = _DEFAULT_IDENTITY_THRESHOLD,
) -> SemanticMatch | None:
    """Return the best semantic match, or an explicit ambiguous result."""
    if not new_instruction:
        return None
    new_tokens = _normalize_tokens(new_instruction)
    if not new_tokens:
        return None

    matches: list[tuple[Directive, float]] = []
    for existing in existing_conventions:
        if exclude_identity_key and existing.identity_key == exclude_identity_key:
            continue
        if not existing.instruction:
            continue
        existing_tokens = _normalize_tokens(existing.instruction)
        if not existing_tokens:
            continue
        similarity = _jaccard_similarity(new_tokens, existing_tokens)
        if similarity >= threshold:
            matches.append((existing, similarity))

    if not matches:
        return None

    matches.sort(key=lambda item: (-item[1], item[0].id))
    best_directive, best_similarity = matches[0]
    tied = [
        directive.id
        for directive, similarity in matches
        if abs(similarity - best_similarity) < 1e-9
    ]
    if len(tied) > 1:
        return SemanticMatch(
            directive_id=None,
            identity_key=None,
            similarity=best_similarity,
            ambiguous=True,
            candidate_ids=tuple(tied),
        )

    return SemanticMatch(
        directive_id=best_directive.id,
        identity_key=best_directive.identity_key,
        similarity=best_similarity,
    )


def check_semantic_dedup(
    new_instruction: str,
    existing_conventions: list["Directive"],
    *,
    exclude_identity_key: str | None = None,
) -> DedupResult:
    """Check a new convention instruction against existing active conventions.

    Args:
        new_instruction: the proposed convention's instruction text.
        existing_conventions: active conventions for the same project
            (from DirectivesRepository.get_active_conventions).
        exclude_identity_key: if provided, skip existing conventions with
            this identity_key. Used by the aggregator to avoid dedup-vs-self
            when UPSERT would update an existing row with the same key.

    Returns:
        DedupResult with verdict:
        - ADD: no significant overlap found; add the convention.
        - SKIP: new instruction is a semantic duplicate of an existing one.
        - SUPERSEDE: new instruction is more precise; supersede the match.
    """
    best = _best_semantic_match(
        new_instruction,
        existing_conventions,
        exclude_identity_key=exclude_identity_key,
        threshold=DEDUP_SIMILARITY_THRESHOLD,
    )
    if not new_instruction or _normalize_tokens(new_instruction) == set():
        return DedupResult(verdict=DedupVerdict.SKIP, similarity=0.0)
    if best is None:
        return DedupResult(verdict=DedupVerdict.ADD, similarity=0.0)

    best_match, best_similarity = best

    new_len = len(new_instruction)
    existing_len = len(best_match.instruction)

    if new_len > existing_len:
        _logger.info(
            "semantic_dedup: new convention supersedes directive_id=%r "
            "(similarity=%.3f, new_len=%d > existing_len=%d)",
            best_match.id,
            best_similarity,
            new_len,
            existing_len,
        )
        return DedupResult(
            verdict=DedupVerdict.SUPERSEDE,
            matched_directive_id=best_match.id,
            similarity=best_similarity,
        )

    _logger.info(
        "semantic_dedup: skipping duplicate convention (similarity=%.3f, matched directive_id=%r)",
        best_similarity,
        best_match.id,
    )
    return DedupResult(
        verdict=DedupVerdict.SKIP,
        matched_directive_id=best_match.id,
        similarity=best_similarity,
    )


__all__ = [
    "DEDUP_SIMILARITY_THRESHOLD",
    "DedupResult",
    "DedupVerdict",
    "SemanticMatch",
    "find_semantic_match",
    "check_semantic_dedup",
]
