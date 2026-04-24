"""
Unit tests for Task 9: Fallback Design.

These tests verify the structural completeness and internal consistency of
the fallback-design.md document. They check that:

  - All required sections are present
  - All three fallback levels are covered
  - Phase impact is addressed per level
  - Agent-specific paths are covered
  - Recommendations are present and explicitly bounded
  - The document uses honest framing (not optimistic language)

EXECUTION ORDER: death tests first (test_fallback_design_death_cases.py),
then these unit tests. Both must pass green before the design is accepted.
"""

import re
import pytest
from conftest import load_fallback_design as load_design


# ---------------------------------------------------------------------------
# Structure: Required top-level sections
# ---------------------------------------------------------------------------

class TestDocumentStructure:
    """Verify the document has all required top-level sections."""

    def test_has_injection_path_catalog_section(self):
        """Step 1: Catalog all injection paths and their verdicts from Task 4."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "injection path", "injection surface", "injection catalog", "task 4",
            "injection verdict"
        ]), "Missing injection path catalog section (Step 1 requirement)"

    def test_has_fallback_levels_section(self):
        """Design must have explicit coverage of fallback levels."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "fallback level", "fallback hierarchy", "fallback strategy",
            "level 1", "level 2", "level 3", "fb-1", "fb-2", "fb-3",
            "runtime injection", "session-start", "observation only"
        ]), "Missing fallback levels section"

    def test_has_recommendations_section(self):
        """Step 8: Design must include a recommendations section."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "recommendation", "recommend", "proposed", "decision", "guidance"
        ]), "Missing recommendations section (Step 8 requirement)"

    def test_has_phase_impact_section(self):
        """Step 7: Design must address phase-by-phase impact of each fallback level."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "phase 1", "phase 2", "phase 3", "phase impact", "per phase"
        ]), "Missing phase impact section (Step 7 requirement)"

    def test_has_decision_criteria_section(self):
        """Step 6: Design must define when each fallback level is chosen."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "decision criteria", "when to", "criteria", "trigger", "choose"
        ]), "Missing decision criteria section (Step 6 requirement)"


# ---------------------------------------------------------------------------
# Coverage: Three fallback levels must be covered
# ---------------------------------------------------------------------------

class TestFallbackLevelCoverage:
    """Each fallback level must be explicitly addressed."""

    def test_runtime_injection_level_covered(self):
        """Level 1 (Runtime injection — full capability) must be covered."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "runtime injection", "level 1", "fb-1", "full capability",
            "real-time", "mid-session"
        ]), "Fallback Level 1 (runtime injection) not covered"

    def test_session_start_only_level_covered(self):
        """Level 2 (Session-start injection only) must be covered."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "session-start-only", "session_start_only", "session start only",
            "level 2", "fb-2", "session-start only", "next session"
        ]), "Fallback Level 2 (session-start only) not covered"

    def test_no_injection_level_covered(self):
        """Level 3 (No injection — observation + analysis only) must be covered."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "no injection", "observation only", "analysis only",
            "observation + analysis", "level 3", "fb-3",
            "observation-only", "no feedback"
        ]), "Fallback Level 3 (no injection / observation only) not covered"


# ---------------------------------------------------------------------------
# Coverage: All three agents mentioned
# ---------------------------------------------------------------------------

class TestAgentCoverage:
    """Each agent's specific injection context must appear in the design."""

    def test_claude_code_addressed(self):
        doc = load_design()
        assert "claude code" in doc.lower() or "claude_code" in doc.lower(), (
            "Claude Code not addressed in fallback design"
        )

    def test_opencode_addressed(self):
        doc = load_design()
        assert "opencode" in doc.lower(), (
            "OpenCode not addressed in fallback design"
        )

    def test_codex_addressed(self):
        doc = load_design()
        assert "codex" in doc.lower(), (
            "Codex not addressed in fallback design"
        )


# ---------------------------------------------------------------------------
# Coverage: Phase 3 feature impact
# ---------------------------------------------------------------------------

class TestPhase3FeatureImpact:
    """Design must name specific Phase 3 features affected by fallback levels."""

    def test_feedback_layer_mentioned(self):
        """Feedback Layer (Phase 3's core) must appear in the design."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "feedback layer", "feedback loop", "directive generation",
            "feedback mechanism"
        ]), "Feedback Layer not mentioned — Phase 3A core feature impact unaddressed"


# ---------------------------------------------------------------------------
# Directive lifecycle
# ---------------------------------------------------------------------------

class TestDirectiveLifecycle:
    """
    Step 3 requirement: For session-start-only mode, define the directive
    lifecycle (generate after session N, inject before session N+1).
    """

    def test_directive_lifecycle_defined_for_session_start_mode(self):
        """Design must describe how directives flow through sessions."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "session n", "session n+1", "next session", "lifecycle",
            "generate after", "inject before", "post-session", "pre-session",
            "between sessions"
        ]), (
            "Directive lifecycle not defined for session-start mode. "
            "Must describe: generate after session N -> inject before session N+1."
        )


# ---------------------------------------------------------------------------
# Alternative delivery mechanisms for no-injection mode
# ---------------------------------------------------------------------------

class TestNoInjectionDelivery:
    """
    Step 4: For no-injection mode, alternative delivery mechanisms must be defined.
    """

    def test_no_injection_alternative_delivery_defined(self):
        """Design must propose alternative delivery for observation-only mode."""
        doc = load_design()
        doc_lower = doc.lower()
        assert any(s in doc_lower for s in [
            "dashboard", "cli report", "report", "human review",
            "external channel", "file output", "manual", "offline"
        ]), (
            "No alternative delivery mechanism defined for no-injection mode. "
            "Step 4 requires: dashboard / CLI report / file output / human review."
        )


# ---------------------------------------------------------------------------
# Acceptance criteria coverage
# ---------------------------------------------------------------------------

class TestAcceptanceCriteriaCoverage:
    """
    Acceptance criteria from the task spec:
    1. Covers: 'Degradation - runtime injection infeasible, session-start only'
    2. Covers: 'Degradation - directive comprehension below 50% but above 30%'
    """

    def test_covers_runtime_infeasible_session_start_only_scenario(self):
        """Must address the scenario: runtime injection infeasible, session-start only."""
        doc = load_design()
        doc_lower = doc.lower()
        runtime_infeasible = any(s in doc_lower for s in [
            "runtime injection infeasible", "runtime not viable",
            "runtime injection is not", "no runtime injection",
            "if runtime", "runtime fails", "session-start only"
        ])
        assert runtime_infeasible, (
            "Acceptance criteria gap: Does not cover 'runtime injection infeasible, "
            "session-start only' degradation scenario."
        )

    def test_covers_partial_compliance_30_to_50_percent_scenario(self):
        """Must address the scenario: compliance 30-50% (below threshold but above floor)."""
        doc = load_design()
        doc_lower = doc.lower()
        partial_compliance = any(s in doc_lower for s in [
            "30%", "50%", "30 to 50", "30-50", "between 30", "below 50",
            "partial compliance", "low compliance", "compliance below"
        ])
        assert partial_compliance, (
            "Acceptance criteria gap: Does not cover 'directive comprehension below 50% "
            "but above 30%' degradation scenario."
        )


# ---------------------------------------------------------------------------
# Honest framing: document must use uncertainty markers, not optimistic certainty
# ---------------------------------------------------------------------------

class TestHonestFraming:
    """
    The document must not make unqualified claims about fallback viability.
    Every viability claim must be bounded by what is verified vs assumed.
    """

    def test_document_uses_uncertainty_markers(self):
        """
        The document must use hedging/uncertainty language to mark what is
        assumption vs evidence — not present everything as settled.
        """
        doc = load_design()
        doc_lower = doc.lower()
        uncertainty_markers = any(s in doc_lower for s in [
            "assumption", "unverified", "pending", "hypothesis", "estimated",
            "depends on", "requires validation", "not yet", "subject to",
            "if phase 1"
        ])
        assert uncertainty_markers, (
            "Document uses no uncertainty markers. Every viability claim is presented as "
            "settled fact. The fallback design depends on unverified assumptions from "
            "Tasks 4 and 6 — those uncertainties must be visible in the text."
        )

    def test_document_does_not_declare_observation_only_as_equivalent_to_full_product(self):
        """
        Observation-only mode must not be framed as equivalent to the full product.
        It must be explicitly called out as losing the core differentiator.
        """
        doc = load_design()
        doc_lower = doc.lower()
        false_equivalence_phrases = [
            "still the full product",
            "full value in observation",
            "observation only is complete",
            "no meaningful difference",
            "almost as good",
            "nearly equivalent",
        ]
        for phrase in false_equivalence_phrases:
            assert phrase not in doc_lower, (
                f"Document uses false equivalence framing: '{phrase}'. "
                "Observation-only mode loses the feedback loop — the core differentiator. "
                "The design must not claim observation-only is equivalent to the full product."
            )

        fb3_loses_differentiation = any(s in doc_lower for s in [
            "not the intended product",
            "loses its core positioning",
            "differentiation is narrow",
            "does not retain full differentiation",
        ])
        assert fb3_loses_differentiation, (
            "Document does not explicitly state that FB-3 (observation-only) loses the "
            "core market differentiation. The absence of negative framing is itself a "
            "form of false equivalence — if the downside is not named, the reader "
            "infers the modes are comparable."
        )
