"""
Death tests for Task 6: Directive Comprehension Experiment.

These tests verify that the investigation document (directive-comprehension.yaml)
explicitly addresses the three specified death cases:

  DC-A: Agent acknowledges directive verbally but does not change behavior
  DC-B: Agent follows directive initially but drops it after context compaction
  DC-C: Agent misinterprets directive (e.g., "avoid redundant reads" -> stops reading files entirely)

EXECUTION ORDER: death tests before unit tests, per samsara protocol.
Samsara mandate: test silent failure paths FIRST.
"""

import yaml
import os
import pytest

YAML_PATH = os.path.join(
    os.path.dirname(__file__),
    "directive-comprehension.yaml"
)

REQUIRED_AGENTS = {"claude_code", "opencode", "codex"}


def load_findings():
    """Load investigation YAML. Fails loudly if file does not exist."""
    with open(YAML_PATH, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# DEATH TEST DC-A: Verbal acknowledgment without behavioral change
# The core death case for this entire experiment.
# ---------------------------------------------------------------------------

class TestDCA_AcknowledgmentWithoutBehavior:
    """
    Death case: Agent says "I'll follow this directive" but does not change
    behavior. Compliance measured by acknowledgment, not observation.
    This is the silent failure mode that makes all injection work worthless.
    """

    def test_compliance_measurement_is_behavioral_not_verbal(self):
        """
        The YAML must define 'complied' as behavioral observation, NOT as
        verbal acknowledgment. If compliance is defined as "agent said it
        would comply," this death case is guaranteed to be missed.
        """
        data = load_findings()
        measurement = data.get("measurement_criteria", {})
        complied_def = str(measurement.get("complied", "")).lower()
        ignored_def = str(measurement.get("ignored", "")).lower()
        misinterpreted_def = str(measurement.get("misinterpreted", "")).lower()

        # Must NOT define compliance as verbal acknowledgment alone
        verbal_only_keywords = ["said", "stated", "acknowledged", "confirmed verbally",
                                "responded with", "replied", "told"]
        behavioral_keywords = ["behav", "action", "observe", "measur", "actual",
                               "did not", "reduced", "increased", "changed",
                               "count", "frequen", "verify", "evidence", "quantif"]

        has_verbal_only = any(kw in complied_def for kw in verbal_only_keywords)
        has_behavioral = any(kw in complied_def for kw in behavioral_keywords)

        assert has_behavioral, (
            "DEATH CASE DC-A: 'complied' definition does not reference behavioral observation. "
            "A directive comprehension test that measures compliance by agent acknowledgment "
            "('I'll follow this') will silently miss the case where the agent acknowledges "
            "but does not actually change behavior. "
            f"Current 'complied' definition: '{complied_def}'"
        )

    def test_measurement_criteria_distinguishes_acknowledgment_from_compliance(self):
        """
        The measurement criteria must explicitly distinguish between
        verbal acknowledgment and behavioral compliance.
        """
        data = load_findings()
        all_text = str(data).lower()

        has_distinction = any(kw in all_text for kw in [
            "acknowledg", "verbal", "says it will", "stated compliance",
            "self-report", "not acknowledgment", "not just acknowledg"
        ])

        assert has_distinction, (
            "DEATH CASE DC-A: Investigation does not distinguish between "
            "verbal acknowledgment and behavioral compliance. "
            "The measurement_criteria section must explicitly state that "
            "self-reported compliance is NOT sufficient evidence. "
            "Behavioral change must be independently observable."
        )

    def test_results_contain_evidence_field_per_observation(self):
        """
        Every result entry must have an evidence field — a description of
        what was actually OBSERVED, not what the agent claimed.
        """
        data = load_findings()
        results = data.get("results", [])
        assert len(results) > 0, (
            "DEATH CASE DC-A: No results entries found. "
            "Must have at least one result entry with observed evidence."
        )
        for i, result in enumerate(results):
            assert "evidence" in result, (
                f"DEATH CASE DC-A: Result entry {i} (agent={result.get('agent')}, "
                f"directive={result.get('directive')}) is missing 'evidence' field. "
                "Evidence must record what was observed, not what was claimed."
            )
            evidence_text = str(result.get("evidence", "")).strip()
            assert evidence_text != "", (
                f"DEATH CASE DC-A: Result entry {i} has empty evidence field. "
                "Empty evidence means compliance is unmeasurable."
            )

    def test_failure_modes_section_names_acknowledgment_death_case(self):
        """
        The failure_modes section must explicitly address the
        acknowledgment-without-behavior failure mode.
        """
        data = load_findings()
        failure_modes = data.get("failure_modes", {})
        all_failure_text = str(failure_modes).lower()

        has_acknowledgment_case = any(kw in all_failure_text for kw in [
            "acknowledg", "verbal compliance", "says it will",
            "self-report", "no behavior change", "behavior unchanged"
        ])

        assert has_acknowledgment_case, (
            "DEATH CASE DC-A: failure_modes section does not address the "
            "acknowledgment-without-behavior pattern. "
            "This is the most common and most silent failure mode in directive compliance. "
            "It must be named explicitly in failure_modes."
        )


# ---------------------------------------------------------------------------
# DEATH TEST DC-B: Directive drop after context compaction
# The most dangerous failure because it's long-latency and session-length
# dependent — the agent appears to comply in early turns, then silently stops.
# ---------------------------------------------------------------------------

class TestDCB_CompactionDrop:
    """
    Death case: Agent follows directive in turns 1-10, but after context
    compaction in a long session, the directive is no longer in active context
    and the agent silently reverts to default behavior.
    """

    def test_session_length_stability_addressed(self):
        """
        The investigation must explicitly address whether directive compliance
        is stable across session length, especially across compaction events.
        """
        data = load_findings()
        assumptions = data.get("assumptions", [])
        all_text = str(data).lower()

        has_session_length = any(kw in all_text for kw in [
            "session length", "long session", "compaction", "compact",
            "context window", "later turns", "turn count", "context drop",
            "stability across"
        ])

        assert has_session_length, (
            "DEATH CASE DC-B: Investigation does not address session-length stability. "
            "An agent may follow a directive for the first 5 turns then silently revert "
            "after context compaction or context window pressure. "
            "The assumptions or failure_modes section must name this explicitly."
        )

    def test_compaction_death_case_named_in_failure_modes(self):
        """
        failure_modes must explicitly mention compaction as a failure scenario.
        """
        data = load_findings()
        failure_modes = data.get("failure_modes", {})
        all_failure_text = str(failure_modes).lower()

        has_compaction = any(kw in all_failure_text for kw in [
            "compact", "context window", "session length", "drops directive",
            "long session", "context overflow"
        ])

        assert has_compaction, (
            "DEATH CASE DC-B: failure_modes does not name compaction as a failure scenario. "
            "After context compaction, directives injected at session-start may be "
            "summarized away or truncated, causing silent behavioral reversion."
        )

    def test_session_length_assumption_is_registered(self):
        """
        The assumption that directive compliance is stable across session length
        must be registered as an assumption with a verified status.
        """
        data = load_findings()
        assumptions = data.get("assumptions", [])

        stability_assumptions = [
            a for a in assumptions
            if any(kw in str(a.get("assumption", "")).lower() for kw in [
                "session length", "stable", "compaction", "across session",
                "later turns", "consistent"
            ])
        ]

        assert len(stability_assumptions) > 0, (
            "DEATH CASE DC-B: No assumption registered about directive compliance "
            "stability across session length. "
            "This must be an explicit, tracked assumption with verified status. "
            "If unverified, downstream phases will assume directives are persistent "
            "when they may not be."
        )


# ---------------------------------------------------------------------------
# DEATH TEST DC-C: Misinterpretation causing overcorrection
# Agent "complies" but interprets directive in a way that breaks functionality.
# ---------------------------------------------------------------------------

class TestDCC_Misinterpretation:
    """
    Death case: Agent follows the directive but interprets it in an unintended
    way that causes a different kind of failure. Example: "avoid redundant reads"
    directive causes agent to stop reading files at all, even when needed.
    This is a "complied but wrong" case that looks like success on naive metrics.
    """

    def test_misinterpreted_category_exists_in_measurement(self):
        """
        The measurement criteria must have a 'misinterpreted' category distinct
        from 'ignored' — not all non-compliance is the same.
        """
        data = load_findings()
        measurement = data.get("measurement_criteria", {})

        assert "misinterpreted" in measurement, (
            "DEATH CASE DC-C: measurement_criteria lacks 'misinterpreted' category. "
            "Without this category, overcorrection looks like compliance ('agent changed behavior') "
            "instead of being flagged as misinterpretation."
        )

    def test_misinterpreted_definition_covers_overcorrection(self):
        """
        The 'misinterpreted' definition must cover the case where agent changes
        behavior in the wrong direction (overcorrection).
        """
        data = load_findings()
        measurement = data.get("measurement_criteria", {})
        misinterpreted_def = str(measurement.get("misinterpreted", "")).lower()

        overcorrection_keywords = [
            "overcorrect", "overreact", "too aggressively", "wrong direction",
            "unintended", "misinterpret", "literal", "excessive", "stop entirely",
            "refused", "avoids", "unnecessary"
        ]

        has_overcorrection = any(kw in misinterpreted_def for kw in overcorrection_keywords)

        assert has_overcorrection, (
            "DEATH CASE DC-C: 'misinterpreted' definition does not cover overcorrection. "
            "The most common misinterpretation failure is the agent applying the directive "
            "too broadly (e.g., 'avoid redundant reads' -> avoids all reads). "
            f"Current 'misinterpreted' definition: '{misinterpreted_def}'"
        )

    def test_misinterpretation_failure_mode_addressed(self):
        """
        failure_modes must explicitly name the misinterpretation pattern.
        """
        data = load_findings()
        failure_modes = data.get("failure_modes", {})
        all_failure_text = str(failure_modes).lower()

        has_misinterp = any(kw in all_failure_text for kw in [
            "misinterpret", "overcorrect", "too broadly", "too literal",
            "wrong direction", "unintended behavior", "interprets differently"
        ])

        assert has_misinterp, (
            "DEATH CASE DC-C: failure_modes does not address directive misinterpretation. "
            "An agent that 'complies' by interpreting a directive too broadly "
            "can produce worse outcomes than ignoring the directive entirely."
        )

    def test_at_least_one_result_with_misinterpreted_status(self):
        """
        Results must include at least one entry showing a misinterpretation
        scenario (or a protocol entry designing how to detect one).
        This ensures the experiment actually looks for this failure mode.
        """
        data = load_findings()
        results = data.get("results", [])

        misinterpreted_results = [
            r for r in results
            if r.get("compliance") == "misinterpreted"
        ]

        protocol_text = str(data.get("experiment_protocol", {})).lower()
        has_misinterp_in_protocol = any(kw in protocol_text for kw in [
            "misinterpret", "overcorrect", "wrong direction", "too broad"
        ])

        assert len(misinterpreted_results) > 0 or has_misinterp_in_protocol, (
            "DEATH CASE DC-C: Neither results entries with compliance=misinterpreted "
            "nor experiment_protocol entries addressing misinterpretation were found. "
            "The experiment must actively probe for this failure mode, not just passively "
            "allow it to be classified if it happens to appear."
        )


# ---------------------------------------------------------------------------
# DEATH TEST: Behavioral measurement vs simple directive tests
# Risk: testing trivially simple directives and extrapolating to complex ones.
# ---------------------------------------------------------------------------

class TestDCGeneral_ComplexityExtrapolation:
    """
    Death case: Experiment only tests simple directives like "always add a comment"
    (with easily verifiable outputs) and extrapolates conclusions to complex behavioral
    directives like trigger-condition-based behavioral changes.
    """

    def test_test_directives_include_trigger_condition_directives(self):
        """
        At least one test directive must include a trigger condition, not just
        a simple always-apply instruction.
        """
        data = load_findings()
        test_directives = data.get("test_directives", [])

        assert len(test_directives) >= 3, (
            "DEATH CASE: Fewer than 3 test directives defined. "
            "Must test at least 3 directives as specified."
        )

        directives_with_trigger = [
            d for d in test_directives
            if d.get("trigger") and str(d.get("trigger", "")).strip() not in ["", "always", "none"]
        ]

        assert len(directives_with_trigger) >= 2, (
            "DEATH CASE: Fewer than 2 test directives have non-trivial trigger conditions. "
            "A test suite with only always-apply directives cannot validate whether "
            "agents can identify trigger conditions and fire directives conditionally. "
            "At least 2 directives must have specific trigger conditions."
        )

    def test_per_agent_compliance_rates_are_tracked(self):
        """
        The summary must track compliance per agent, not just an overall rate.
        Without per-agent breakdown, a poor-performing agent is hidden by
        averages from agents with higher compliance.
        """
        data = load_findings()
        summary = data.get("summary", {})
        per_agent = summary.get("per_agent", {})

        for agent in REQUIRED_AGENTS:
            assert agent in per_agent, (
                f"DEATH CASE: Per-agent compliance rate for '{agent}' is missing from summary. "
                "A single compliance rate averages over all agents. If one agent has 10% compliance "
                "and another has 90%, the average hides a critical failure."
            )

    def test_per_phrasing_compliance_rates_are_tracked(self):
        """
        The summary must track compliance per phrasing style.
        Without phrasing breakdown, the recommended phrasing has no evidence basis.
        """
        data = load_findings()
        summary = data.get("summary", {})
        per_phrasing = summary.get("per_phrasing", {})

        required_phrasings = {"formal_json", "natural_language", "concise_imperative"}
        for phrasing in required_phrasings:
            assert phrasing in per_phrasing, (
                f"DEATH CASE: Per-phrasing compliance rate for '{phrasing}' is missing. "
                "The recommended_phrasing field cannot be justified without per-phrasing data."
            )

    def test_recommended_phrasing_is_evidence_based(self):
        """
        The recommended_phrasing must be one of the tested phrasing styles,
        demonstrating it comes from the data, not from prior assumption.
        """
        data = load_findings()
        summary = data.get("summary", {})
        recommended = summary.get("recommended_phrasing", "")
        per_phrasing = summary.get("per_phrasing", {})

        assert recommended, (
            "DEATH CASE: recommended_phrasing is empty. "
            "Without a recommendation, Phase 3A has no evidence-based starting point."
        )

        valid_phrasings = {"formal_json", "natural_language", "concise_imperative"}
        phrasing_mentioned = any(p in recommended for p in valid_phrasings)
        assert phrasing_mentioned, (
            "DEATH CASE: recommended_phrasing does not name any tested phrasing style. "
            "Phase 0 recommendations are hypotheses, but must reference a tested style. "
            f"Valid styles: {valid_phrasings}. Got: '{recommended}'"
        )
