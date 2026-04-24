"""
Death tests for Task 9: Fallback Design.

These tests verify that the fallback design document (fallback-design.md) does NOT
commit the following silent failures:

  FD-1: Claims "session-start-only mode is viable" without quantifying which Phase 3
        features are dropped. Viability without scope impact is a false claim.

  FD-2: Assumes "observation + analysis only" is still a differentiated product
        without re-evaluating market positioning against LangSmith/Langfuse/OTel.

  FD-3: Proposes workarounds that have NOT been validated against agent behavior
        (i.e., references things marked not_tested or unverified in Tasks 4 and 6).

  FD-4: States a degraded mode is "viable" without specifying the decision criteria
        for choosing that mode over a higher or lower fallback level.

  FD-5: Assumes directive comprehension below 50% but above 30% still produces a
        functional feedback loop without measuring what "functional" means at that
        compliance level.

EXECUTION ORDER: death tests before unit tests, per samsara protocol.
"""

import re
import pytest
from conftest import load_fallback_design as load_design


# ---------------------------------------------------------------------------
# DEATH TEST FD-1: Session-start-only viability without feature-loss accounting
# ---------------------------------------------------------------------------

class TestFD1_SessionStartViabilityWithoutFeatureLoss:
    """
    Death case: Design says "session-start-only mode is viable" without listing
    which specific PRD features are lost or degraded. This is the most dangerous
    optimism bias in fallback design — accepting a degraded mode without measuring
    the degradation.
    """

    @staticmethod
    def _extract_fb2_section(doc: str) -> str:
        """Extract the FB-2 section from the document.

        Looks for the FB-2 heading and captures content until the next
        same-level heading or end of document.
        """
        fb2_pattern = re.compile(
            r"^###\s+.*\bFB-2\b.*$",
            re.MULTILINE | re.IGNORECASE,
        )
        match = fb2_pattern.search(doc)
        assert match, (
            "DEATH CASE FD-1: No '### ... FB-2 ...' heading found. "
            "The design must have a dedicated section for Fallback Level 2."
        )
        rest = doc[match.end():]
        next_heading = re.search(r"^###\s", rest, re.MULTILINE)
        if next_heading:
            return rest[:next_heading.start()]
        return rest

    def test_document_names_features_lost_in_session_start_only_mode(self):
        """
        The design must have a structured feature-by-feature impact table
        for session-start-only (FB-2) showing which features are LOST vs AVAILABLE.
        A vague mention of "lost" is insufficient — the table must pair feature
        names with status labels. The table must appear within the FB-2 section,
        not elsewhere in the document.
        """
        doc = load_design()
        fb2_section = self._extract_fb2_section(doc).lower()

        has_fb2_feature_table = (
            "| feature" in fb2_section
            and "lost" in fb2_section
            and "available" in fb2_section
        )
        specific_features_named = sum(1 for feat in [
            "real-time", "mid-session", "mcp", "directive",
            "waste detection", "runtime"
        ] if feat in fb2_section)

        assert has_fb2_feature_table and specific_features_named >= 3, (
            "DEATH CASE FD-1: Design claims session-start-only mode is viable but "
            "does not provide a structured feature-by-feature impact table with "
            "LOST/AVAILABLE labels within the FB-2 section. Without concrete feature "
            "enumeration scoped to FB-2, 'viable' is an empty claim. Must name at "
            "least 3 specific features with their status. "
            f"Found {specific_features_named} feature names in FB-2 section."
        )

    def test_document_includes_session_start_only_section(self):
        """
        The design must have an explicit section for session-start-only fallback,
        not just a single-line mention.
        """
        doc = load_design()
        doc_lower = doc.lower()

        session_start_section = any(s in doc_lower for s in [
            "session-start-only",
            "session_start_only",
            "session start only",
            "fallback level 2",
            "level 2",
            "fb-2",
        ])

        assert session_start_section, (
            "DEATH CASE FD-1: No dedicated section for session-start-only fallback found. "
            "A one-line mention is insufficient. The design must have a section that "
            "covers: what SecondSight CAN do, what it CANNOT do, and which features "
            "are affected at this fallback level."
        )

    def test_session_start_only_section_addresses_directive_latency(self):
        """
        Session-start-only mode introduces a one-session delay between observing
        a behavior and correcting it. The design MUST acknowledge this delay
        and its impact — it is the primary functional gap vs runtime injection.
        """
        doc = load_design()
        doc_lower = doc.lower()

        latency_acknowledgment = any(s in doc_lower for s in [
            "one session", "next session", "session delay", "delayed by",
            "session n+1", "session n + 1", "lag", "latency between",
            "one-session delay", "previous session"
        ])

        assert latency_acknowledgment, (
            "DEATH CASE FD-1: Session-start-only mode has a critical latency property: "
            "directives from session N analysis cannot reach the agent until session N+1. "
            "The design does not acknowledge this one-session lag. This gap matters for "
            "real-time feedback use cases. If the design doesn't address it, any claim "
            "of viability is incomplete."
        )


# ---------------------------------------------------------------------------
# DEATH TEST FD-2: Observation-only mode assumed viable without market re-evaluation
# ---------------------------------------------------------------------------

class TestFD2_ObservationOnlyProductViability:
    """
    Death case: Design accepts observation-only as a fallback product without
    re-evaluating whether SecondSight retains market differentiation against
    established observation tools (LangSmith, Langfuse, OpenTelemetry).
    """

    def test_document_names_competing_observation_tools(self):
        """
        If the design addresses the observation-only fallback, it MUST name
        the competing tools and assess whether SecondSight is still differentiated.
        Accepting observation-only without naming the competition is a market analysis gap.
        """
        doc = load_design()
        doc_lower = doc.lower()

        names_competition = any(s in doc_lower for s in [
            "langsmith", "langfuse", "opentelemetry", "observability",
            "observation tool", "existing tool", "market", "differentiat",
            "competitor"
        ])

        assert names_competition, (
            "DEATH CASE FD-2: Design addresses observation-only fallback but does not "
            "name or assess competing observation tools (LangSmith, Langfuse, OpenTelemetry). "
            "SecondSight's differentiation IS the feedback loop. In observation-only mode, "
            "SecondSight competes directly with established platforms. This must be addressed "
            "explicitly — either 'observation-only is still differentiated because X' or "
            "'observation-only loses our market position, do not enter this fallback.'"
        )

    def test_document_addresses_observation_only_product_viability_explicitly(self):
        """
        The design must explicitly assess whether observation-only SecondSight
        is a viable product — not just technically feasible but worth building.
        """
        doc = load_design()
        doc_lower = doc.lower()

        viability_assessment = any(s in doc_lower for s in [
            "viable product", "product viability", "still differentiat",
            "market position", "no longer differentiat", "not differentiat",
            "market gap", "value proposition", "still worth", "no longer viable"
        ])

        assert viability_assessment, (
            "DEATH CASE FD-2: Design does not assess observation-only product viability. "
            "Technical feasibility (can we observe) is not the same as product viability "
            "(is it worth building relative to alternatives). The fallback design must "
            "explicitly answer: at what fallback level does SecondSight lose its "
            "market differentiation?"
        )


# ---------------------------------------------------------------------------
# DEATH TEST FD-3: Workarounds referencing unvalidated agent behavior
# ---------------------------------------------------------------------------

class TestFD3_UnvalidatedWorkarounds:
    """
    Death case: Design proposes fallback workarounds (e.g., 'MCP pull-based runtime
    injection as complement') that depend on agent behaviors marked not_tested or
    unverified in Tasks 4 and 6. Proposing unvalidated workarounds as fallback
    paths gives false confidence.
    """

    def test_document_distinguishes_verified_vs_unverified_paths(self):
        """
        Any path proposed as a fallback workaround must be labeled as verified or
        unverified. The design must not present unverified paths as reliable fallbacks.
        """
        doc = load_design()
        doc_lower = doc.lower()

        verification_labeling = any(s in doc_lower for s in [
            "verified", "unverified", "not tested", "not_tested", "hypothesis",
            "untested", "requires validation", "pending", "assumed", "assumption"
        ])

        assert verification_labeling, (
            "DEATH CASE FD-3: Design proposes fallback paths but does not label which "
            "are verified vs unverified. Presenting unverified paths alongside verified "
            "ones without distinction gives false confidence in fallback reliability. "
            "Each proposed path must be marked as verified, partially_verified, or unverified."
        )

    def test_mcp_path_noted_as_pull_based_not_push(self):
        """
        If MCP is mentioned as a fallback path, the design must note it is pull-based
        (agent must call it) — not push-based. Presenting MCP as 'runtime injection'
        without this distinction is technically misleading.
        """
        doc = load_design()
        doc_lower = doc.lower()

        # Only check if MCP is mentioned at all
        if "mcp" not in doc_lower:
            pytest.skip("MCP not mentioned in design — test not applicable")

        pull_based_noted = any(s in doc_lower for s in [
            "pull-based", "pull based", "agent must call", "agent decides",
            "not push", "cannot push", "pull not push", "requires agent",
            "agent-initiated"
        ])

        assert pull_based_noted, (
            "DEATH CASE FD-3: MCP is mentioned as a fallback path but the pull-based "
            "limitation is not noted. MCP injection requires the agent to call the tool — "
            "SecondSight cannot push directives mid-session via MCP. If this limitation "
            "is not noted, the design overstates MCP's reliability as a runtime fallback."
        )

    def test_opencode_injection_uncertainty_acknowledged(self):
        """
        OpenCode's injection path was marked as PARTIALLY_VIABLE with UNVERIFIED status
        in Task 4. Any fallback design that includes OpenCode must acknowledge this.
        """
        doc = load_design()
        doc_lower = doc.lower()

        # Only check if OpenCode fallback is addressed
        if "opencode" not in doc_lower:
            pytest.skip("OpenCode not addressed in fallback design — test not applicable")

        opencode_uncertainty = any(s in doc_lower for s in [
            "opencode injection unverified", "opencode config unverified",
            "opencode path unverified", "opencode partially", "unverified",
            "opencode injection path", "not confirmed", "inferred"
        ])

        assert opencode_uncertainty, (
            "DEATH CASE FD-3: Design mentions OpenCode but does not acknowledge that "
            "OpenCode's config injection path was UNVERIFIED in Task 4 (inferred, not "
            "confirmed from official docs). Presenting OpenCode injection as a reliable "
            "fallback path when it hasn't been verified is a false confidence signal."
        )


# ---------------------------------------------------------------------------
# DEATH TEST FD-4: Fallback levels without decision criteria
# ---------------------------------------------------------------------------

class TestFD4_NoCriteriaForChoosingFallbackLevel:
    """
    Death case: Design defines three fallback levels but doesn't tell the team
    when to choose each one. Without decision criteria, fallback levels are just
    labels — the team will default to optimism ("it'll work") rather than making
    an explicit choice based on evidence.
    """

    def test_document_defines_decision_criteria(self):
        """
        The design must define explicit criteria (conditions, thresholds, evidence)
        that determine which fallback level to adopt.
        """
        doc = load_design()
        doc_lower = doc.lower()

        decision_criteria = any(s in doc_lower for s in [
            "decision criteria", "when to choose", "choose this level",
            "adopt this fallback", "trigger condition", "escalate to",
            "fall back to", "if injection fails", "if compliance below",
            "threshold", "criterion"
        ])

        assert decision_criteria, (
            "DEATH CASE FD-4: Design defines fallback levels but does not specify "
            "decision criteria for choosing between them. Without explicit criteria, "
            "the team defaults to the most optimistic path. The design must answer: "
            "what evidence or condition triggers each fallback level?"
        )

    def test_document_addresses_compliance_threshold_scenario(self):
        """
        The acceptance criteria explicitly requires covering:
        'Degradation - directive comprehension below 50% but above 30%'
        The design must address what SecondSight does in this intermediate zone.
        """
        doc = load_design()
        doc_lower = doc.lower()

        compliance_zone = any(s in doc_lower for s in [
            "30%", "50%", "below 50", "above 30", "between 30", "30 and 50",
            "compliance rate", "comprehension rate", "compliance threshold",
            "partial compliance"
        ])

        assert compliance_zone, (
            "DEATH CASE FD-4: Acceptance criteria requires covering 'Degradation - "
            "directive comprehension below 50% but above 30%'. The design does not "
            "address this intermediate compliance zone. At 30-50% compliance, "
            "some directives work and some don't — what does SecondSight do? "
            "Accept it? Filter directives? Fall to a different mode?"
        )


# ---------------------------------------------------------------------------
# DEATH TEST FD-5: Feedback loop at partial compliance — no viability threshold defined
# ---------------------------------------------------------------------------

class TestFD5_FeedbackLoopAtPartialCompliance:
    """
    Death case: Design accepts partial compliance (30-50%) as still enabling a
    feedback loop without measuring what 'functional feedback loop' means at
    that compliance level. If 40% of directives are followed, does SecondSight
    still produce reliable waste reduction? Or does noise dominate the signal?
    """

    def test_document_addresses_feedback_loop_at_partial_compliance(self):
        """
        Design must address whether the feedback loop still functions at
        below-50% directive compliance.
        """
        doc = load_design()
        doc_lower = doc.lower()

        partial_compliance_feedback = any(s in doc_lower for s in [
            "partial compliance", "low compliance", "feedback loop at",
            "still functional", "still viable", "minimum compliance",
            "compliance floor", "requires at least", "sufficient compliance",
            "noise", "signal-to-noise"
        ])

        assert partial_compliance_feedback, (
            "DEATH CASE FD-5: Design does not address whether the feedback loop remains "
            "functional at 30-50% directive compliance. Below 50% means more than half "
            "of directives are ignored. If the noise level is high, agents following some "
            "directives but ignoring others may produce worse behavior than no directives. "
            "The design must define a minimum compliance threshold for a viable feedback loop."
        )

    def test_document_does_not_call_partial_compliance_equivalent_to_full(self):
        """
        The design must NOT claim that session-start-only is 'almost as good as'
        runtime injection without measuring the gap. Optimistic equivalence framing
        is the specific anti-pattern the task spec warns against.
        """
        doc = load_design()
        doc_lower = doc.lower()

        optimistic_equivalence = any(s in doc_lower for s in [
            "almost as good", "nearly equivalent", "minimal loss",
            "only slightly worse", "not much worse",
        ])

        assert not optimistic_equivalence, (
            "DEATH CASE FD-5: Design uses optimistic equivalence framing — claiming "
            "session-start-only mode is 'almost as good as' runtime injection without "
            "measuring the gap. This is the anti-pattern the task spec explicitly names. "
            "The gap must be measured (or explicitly marked unmeasured), not dismissed."
        )
