"""
Unit tests for Task 6: Directive Comprehension Experiment.

These tests validate the structure, completeness, and internal consistency
of directive-comprehension.yaml. They run AFTER death tests pass.

Per samsara protocol: death tests first, unit tests second.
"""

import yaml
import os
import pytest

YAML_PATH = os.path.join(
    os.path.dirname(__file__),
    "directive-comprehension.yaml"
)

REQUIRED_AGENTS = {"claude_code", "opencode", "codex"}
REQUIRED_PHRASINGS = {"formal_json", "natural_language", "concise_imperative"}
VALID_COMPLIANCE_VALUES = {"complied", "ignored", "misinterpreted", "not_tested"}
VALID_VERDICT_VALUES = {"feasible", "partially_feasible", "infeasible"}


def load_findings():
    with open(YAML_PATH, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Document structure
# ---------------------------------------------------------------------------

class TestDocumentStructure:
    """Top-level document must match the output format defined in the task spec."""

    def test_yaml_loads_without_error(self):
        data = load_findings()
        assert data is not None

    def test_required_top_level_fields_present(self):
        data = load_findings()
        required = {
            "investigation_date", "experiment_type", "test_directives",
            "phrasing_variants", "results", "measurement_criteria",
            "summary", "failure_modes", "assumptions", "experiment_protocol",
            "feasibility_verdict"
        }
        missing = required - set(data.keys())
        assert not missing, f"Missing top-level fields: {missing}"

    def test_investigation_date_is_correct(self):
        data = load_findings()
        assert data["investigation_date"] == "2026-04-24", (
            f"Expected 2026-04-24, got {data['investigation_date']}"
        )

    def test_experiment_type_is_correct(self):
        data = load_findings()
        assert data["experiment_type"] == "directive_comprehension"


# ---------------------------------------------------------------------------
# Test directives
# ---------------------------------------------------------------------------

class TestDirectiveDesign:
    """Test directives must be properly formed and representative."""

    def test_at_least_three_test_directives(self):
        data = load_findings()
        directives = data.get("test_directives", [])
        assert len(directives) >= 3, (
            f"Expected >= 3 test directives, got {len(directives)}"
        )

    def test_each_directive_has_required_fields(self):
        data = load_findings()
        required_fields = {"id", "scope", "trigger", "instruction", "expected_behavior_change"}
        for d in data.get("test_directives", []):
            missing = required_fields - set(d.keys())
            assert not missing, (
                f"Directive '{d.get('id', 'unknown')}' missing fields: {missing}"
            )

    def test_directive_ids_are_unique(self):
        data = load_findings()
        ids = [d["id"] for d in data.get("test_directives", [])]
        assert len(ids) == len(set(ids)), f"Duplicate directive IDs: {ids}"

    def test_each_directive_has_non_empty_instruction(self):
        data = load_findings()
        for d in data.get("test_directives", []):
            assert str(d.get("instruction", "")).strip(), (
                f"Directive '{d.get('id')}' has empty instruction"
            )

    def test_each_directive_has_non_empty_expected_behavior_change(self):
        data = load_findings()
        for d in data.get("test_directives", []):
            assert str(d.get("expected_behavior_change", "")).strip(), (
                f"Directive '{d.get('id')}' has empty expected_behavior_change"
            )

    def test_at_least_two_directives_have_non_trivial_triggers(self):
        """Verify complexity of tested directives — not all trivially simple."""
        data = load_findings()
        directives = data.get("test_directives", [])
        non_trivial = [
            d for d in directives
            if str(d.get("trigger", "")).strip() not in ["always", "none", ""]
        ]
        assert len(non_trivial) >= 2, (
            f"Expected >= 2 directives with non-trivial triggers, got {len(non_trivial)}. "
            "Only always-apply directives cannot test trigger identification ability."
        )


# ---------------------------------------------------------------------------
# Phrasing variants
# ---------------------------------------------------------------------------

class TestPhrasingVariants:
    """Three phrasing variants must exist per directive."""

    def test_phrasing_variants_section_exists(self):
        data = load_findings()
        variants = data.get("phrasing_variants", {})
        assert variants, "phrasing_variants section is empty"

    def test_all_directives_have_all_three_phrasings(self):
        data = load_findings()
        variants = data.get("phrasing_variants", {})
        directive_ids = [d["id"] for d in data.get("test_directives", [])]

        for did in directive_ids:
            assert did in variants, (
                f"Directive '{did}' missing from phrasing_variants"
            )
            phrasings = set(variants[did].keys())
            missing = REQUIRED_PHRASINGS - phrasings
            assert not missing, (
                f"Directive '{did}' missing phrasings: {missing}"
            )

    def test_phrasing_texts_are_non_empty(self):
        data = load_findings()
        variants = data.get("phrasing_variants", {})
        for did, phrasing_dict in variants.items():
            for phrasing_type, text in phrasing_dict.items():
                assert str(text).strip(), (
                    f"Phrasing '{phrasing_type}' for directive '{did}' is empty"
                )

    def test_formal_json_phrasing_contains_json_structure(self):
        """formal_json variant must look like JSON (contain braces or key-value syntax)."""
        data = load_findings()
        variants = data.get("phrasing_variants", {})
        for did, phrasing_dict in variants.items():
            formal = str(phrasing_dict.get("formal_json", ""))
            has_json_markers = (
                ("{" in formal and "}" in formal) or
                ('"scope"' in formal) or
                ("scope:" in formal) or
                ("instruction:" in formal)
            )
            assert has_json_markers, (
                f"formal_json phrasing for directive '{did}' does not look like JSON/structured format. "
                f"Got: '{formal[:100]}...'"
            )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class TestResults:
    """Results table must be well-formed."""

    def test_results_list_is_non_empty(self):
        data = load_findings()
        results = data.get("results", [])
        assert len(results) > 0, "Results list is empty"

    def test_each_result_has_required_fields(self):
        data = load_findings()
        required = {"agent", "directive", "phrasing", "compliance", "evidence"}
        for i, r in enumerate(data.get("results", [])):
            missing = required - set(r.keys())
            assert not missing, (
                f"Result entry {i} missing fields: {missing}"
            )

    def test_compliance_values_are_valid(self):
        data = load_findings()
        for r in data.get("results", []):
            compliance = r.get("compliance")
            assert compliance in VALID_COMPLIANCE_VALUES, (
                f"Invalid compliance value '{compliance}'. "
                f"Must be one of {VALID_COMPLIANCE_VALUES}"
            )

    def test_phrasing_values_are_valid(self):
        data = load_findings()
        for r in data.get("results", []):
            phrasing = r.get("phrasing")
            assert phrasing in REQUIRED_PHRASINGS, (
                f"Invalid phrasing value '{phrasing}'. "
                f"Must be one of {REQUIRED_PHRASINGS}"
            )

    def test_agent_values_reference_known_agents(self):
        data = load_findings()
        for r in data.get("results", []):
            agent = r.get("agent")
            assert agent in REQUIRED_AGENTS, (
                f"Unknown agent '{agent}' in results. "
                f"Must be one of {REQUIRED_AGENTS}"
            )

    def test_directive_references_match_defined_directives(self):
        data = load_findings()
        defined_ids = {d["id"] for d in data.get("test_directives", [])}
        for r in data.get("results", []):
            ref = r.get("directive")
            assert ref in defined_ids, (
                f"Result references directive '{ref}' which is not defined in test_directives. "
                f"Defined: {defined_ids}"
            )

    def test_results_cover_all_agents(self):
        """Every agent must appear in results (even if not_tested)."""
        data = load_findings()
        result_agents = {r.get("agent") for r in data.get("results", [])}
        missing = REQUIRED_AGENTS - result_agents
        assert not missing, (
            f"No results entries for agents: {missing}. "
            "All agents must be represented, even if compliance=not_tested."
        )

    def test_results_cover_all_phrasings(self):
        """Every phrasing must appear in results."""
        data = load_findings()
        result_phrasings = {r.get("phrasing") for r in data.get("results", [])}
        missing = REQUIRED_PHRASINGS - result_phrasings
        assert not missing, (
            f"No results entries for phrasings: {missing}. "
            "All phrasing variants must be tested."
        )


# ---------------------------------------------------------------------------
# Measurement criteria
# ---------------------------------------------------------------------------

class TestMeasurementCriteria:
    """Measurement criteria must define all three compliance categories."""

    def test_all_categories_defined(self):
        data = load_findings()
        measurement = data.get("measurement_criteria", {})
        for category in ("complied", "ignored", "misinterpreted"):
            assert category in measurement, (
                f"measurement_criteria missing '{category}' definition"
            )

    def test_all_definitions_are_non_empty(self):
        data = load_findings()
        measurement = data.get("measurement_criteria", {})
        for category, definition in measurement.items():
            assert str(definition).strip(), (
                f"measurement_criteria['{category}'] is empty"
            )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:
    """Summary section must match required structure from task spec."""

    def test_summary_has_required_fields(self):
        data = load_findings()
        summary = data.get("summary", {})
        required = {
            "overall_compliance_rate",
            "per_agent",
            "per_phrasing",
            "recommended_phrasing",
            "verdict"
        }
        missing = required - set(summary.keys())
        assert not missing, f"summary missing fields: {missing}"

    def test_verdict_is_valid(self):
        data = load_findings()
        verdict = data.get("summary", {}).get("verdict")
        assert verdict in VALID_VERDICT_VALUES, (
            f"Invalid verdict '{verdict}'. Must be one of {VALID_VERDICT_VALUES}"
        )

    def test_per_agent_covers_all_agents(self):
        data = load_findings()
        per_agent = data.get("summary", {}).get("per_agent", {})
        missing = REQUIRED_AGENTS - set(per_agent.keys())
        assert not missing, (
            f"summary.per_agent missing agents: {missing}"
        )

    def test_per_phrasing_covers_all_phrasings(self):
        data = load_findings()
        per_phrasing = data.get("summary", {}).get("per_phrasing", {})
        missing = REQUIRED_PHRASINGS - set(per_phrasing.keys())
        assert not missing, (
            f"summary.per_phrasing missing phrasings: {missing}"
        )

    def test_recommended_phrasing_is_non_empty(self):
        data = load_findings()
        rec = data.get("summary", {}).get("recommended_phrasing", "")
        assert str(rec).strip(), "summary.recommended_phrasing is empty"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class TestFailureModes:
    """Failure modes section must exist and have non-trivial content."""

    def test_failure_modes_section_exists_and_is_non_empty(self):
        data = load_findings()
        failure_modes = data.get("failure_modes", {})
        assert failure_modes, "failure_modes section is empty or missing"

    def test_failure_modes_has_at_least_three_entries(self):
        data = load_findings()
        failure_modes = data.get("failure_modes", {})
        if isinstance(failure_modes, dict):
            count = len(failure_modes)
        elif isinstance(failure_modes, list):
            count = len(failure_modes)
        else:
            count = 0
        assert count >= 3, (
            f"failure_modes has only {count} entries. "
            "Must cover at least: acknowledgment-without-behavior, compaction drop, misinterpretation."
        )


# ---------------------------------------------------------------------------
# Assumptions
# ---------------------------------------------------------------------------

class TestAssumptions:
    """Assumptions must be registered with verification status."""

    def test_assumptions_section_is_non_empty(self):
        data = load_findings()
        assumptions = data.get("assumptions", [])
        assert len(assumptions) > 0, "assumptions section is empty"

    def test_each_assumption_has_verified_field(self):
        data = load_findings()
        for a in data.get("assumptions", []):
            assert "verified" in a, (
                f"Assumption '{a.get('assumption', 'unknown')}' missing 'verified' field"
            )

    def test_each_assumption_has_note_field(self):
        data = load_findings()
        for a in data.get("assumptions", []):
            assert "note" in a, (
                f"Assumption '{a.get('assumption', 'unknown')}' missing 'note' field"
            )


# ---------------------------------------------------------------------------
# Experiment protocol
# ---------------------------------------------------------------------------

class TestExperimentProtocol:
    """Protocol section documents how experiments would be run."""

    def test_protocol_section_is_non_empty(self):
        data = load_findings()
        protocol = data.get("experiment_protocol", {})
        assert protocol, "experiment_protocol section is empty"

    def test_protocol_names_injection_method_per_agent(self):
        """Protocol must specify which injection method to use per agent."""
        data = load_findings()
        protocol = data.get("experiment_protocol", {})
        all_protocol_text = str(protocol).lower()

        has_injection_method = any(kw in all_protocol_text for kw in [
            "claude.md", "agents.md", "settings.json", "session_start",
            "injection", "config"
        ])

        assert has_injection_method, (
            "experiment_protocol does not reference injection method. "
            "Protocol must specify how directives are delivered per agent."
        )

    def test_protocol_addresses_measurement_timing(self):
        """Protocol must say when/how behavioral measurements are taken."""
        data = load_findings()
        protocol = data.get("experiment_protocol", {})
        all_protocol_text = str(protocol).lower()

        has_timing = any(kw in all_protocol_text for kw in [
            "after", "turn", "session", "measure", "observe", "count",
            "before and after", "baseline", "compare"
        ])

        assert has_timing, (
            "experiment_protocol does not address measurement timing or method. "
            "Must specify when and how behavioral change is observed."
        )


# ---------------------------------------------------------------------------
# Feasibility verdict
# ---------------------------------------------------------------------------

class TestFeasibilityVerdict:
    """The feasibility_verdict section is the Phase 3A input."""

    def test_feasibility_verdict_exists_and_is_non_empty(self):
        data = load_findings()
        verdict = data.get("feasibility_verdict", {})
        assert verdict, "feasibility_verdict section is empty"

    def test_verdict_names_recommended_phrasing(self):
        data = load_findings()
        verdict = data.get("feasibility_verdict", {})
        recommendation = str(verdict.get("phrasing_recommendation", "")).strip()
        assert recommendation, (
            "feasibility_verdict missing phrasing_recommendation. "
            "Phase 3A needs this to know which phrasing format to use."
        )

    def test_verdict_names_known_risks(self):
        data = load_findings()
        verdict = data.get("feasibility_verdict", {})
        risks = verdict.get("known_risks", [])
        assert len(risks) > 0, (
            "feasibility_verdict has no known_risks. "
            "A verdict without named risks is overconfident."
        )
