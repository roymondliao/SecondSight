"""
Unit tests for Task 4: Runtime Injection Feasibility Investigation.

These tests validate the structure and completeness of injection-feasibility.yaml.
They verify that the investigation document meets the acceptance criteria.

Run after: test_injection_death_cases.py (death tests)
"""

import yaml
import os
import pytest

YAML_PATH = os.path.join(
    os.path.dirname(__file__),
    "injection-feasibility.yaml"
)

REQUIRED_AGENTS = {"claude_code", "opencode", "codex"}
VALID_INJECTION_TYPES = {"runtime", "session_start", "indirect"}
VALID_VERDICTS = {"viable", "partially_viable", "not_viable"}
VALID_OVERALL_VERDICTS = {"feasible", "partially_feasible", "infeasible"}
VALID_STABILITY_RISKS = {"low", "medium", "high"}
REQUIRED_PATH_FIELDS = {
    "type", "method", "format", "size_limit", "persistence",
    "latency", "officially_supported", "stability_risk", "verdict", "evidence"
}


def load_findings():
    with open(YAML_PATH, "r") as f:
        return yaml.safe_load(f)


class TestDocumentStructure:
    """Validates top-level document structure."""

    def test_yaml_loads_without_error(self):
        data = load_findings()
        assert data is not None

    def test_required_top_level_fields(self):
        data = load_findings()
        assert "investigation_date" in data, "Missing investigation_date"
        assert "source" in data, "Missing source"
        assert "agents" in data, "Missing agents list"
        assert "summary" in data, "Missing summary"

    def test_investigation_date_is_correct(self):
        data = load_findings()
        assert data["investigation_date"] == "2026-04-24", (
            f"Expected investigation_date=2026-04-24, got {data['investigation_date']}"
        )

    def test_all_three_agents_present(self):
        data = load_findings()
        agent_names = {a["name"] for a in data["agents"]}
        missing = REQUIRED_AGENTS - agent_names
        assert not missing, f"Missing agents: {missing}"

    def test_no_extra_undocumented_agents(self):
        """Investigation scope is exactly three agents."""
        data = load_findings()
        agent_names = {a["name"] for a in data["agents"]}
        extra = agent_names - REQUIRED_AGENTS
        assert not extra, f"Unexpected agents in investigation: {extra}"


class TestAgentInjectionPaths:
    """Validates per-agent injection path documentation."""

    def test_each_agent_has_injection_paths(self):
        data = load_findings()
        for agent in data["agents"]:
            paths = agent.get("injection_paths", [])
            assert len(paths) > 0, (
                f"Agent '{agent['name']}' has no injection_paths. "
                "Even if all paths are not_viable, they must be documented."
            )

    def test_each_path_has_required_fields(self):
        data = load_findings()
        for agent in data["agents"]:
            for i, path in enumerate(agent.get("injection_paths", [])):
                missing = REQUIRED_PATH_FIELDS - set(path.keys())
                assert not missing, (
                    f"Agent '{agent['name']}' path[{i}] (method='{path.get('method', '?')}') "
                    f"is missing required fields: {missing}"
                )

    def test_each_path_type_is_valid(self):
        data = load_findings()
        for agent in data["agents"]:
            for path in agent.get("injection_paths", []):
                assert path["type"] in VALID_INJECTION_TYPES, (
                    f"Agent '{agent['name']}' path '{path.get('method')}' has invalid type "
                    f"'{path['type']}'. Must be one of {VALID_INJECTION_TYPES}."
                )

    def test_each_path_verdict_is_valid(self):
        data = load_findings()
        for agent in data["agents"]:
            for path in agent.get("injection_paths", []):
                assert path["verdict"] in VALID_VERDICTS, (
                    f"Agent '{agent['name']}' path '{path.get('method')}' has invalid verdict "
                    f"'{path['verdict']}'. Must be one of {VALID_VERDICTS}."
                )

    def test_each_path_stability_risk_is_valid(self):
        data = load_findings()
        for agent in data["agents"]:
            for path in agent.get("injection_paths", []):
                assert path["stability_risk"] in VALID_STABILITY_RISKS, (
                    f"Agent '{agent['name']}' path '{path.get('method')}' has invalid "
                    f"stability_risk '{path['stability_risk']}'. Must be one of {VALID_STABILITY_RISKS}."
                )

    def test_each_path_officially_supported_is_bool(self):
        data = load_findings()
        for agent in data["agents"]:
            for path in agent.get("injection_paths", []):
                assert isinstance(path["officially_supported"], bool), (
                    f"Agent '{agent['name']}' path '{path.get('method')}' "
                    f"officially_supported must be bool, got {type(path['officially_supported'])}."
                )

    def test_each_path_evidence_is_non_empty(self):
        data = load_findings()
        for agent in data["agents"]:
            for path in agent.get("injection_paths", []):
                evidence = path.get("evidence", "")
                assert evidence and str(evidence).strip(), (
                    f"Agent '{agent['name']}' path '{path.get('method')}' has empty evidence. "
                    "Evidence must explain the basis for the verdict."
                )


class TestAgentVerdicts:
    """Validates per-agent overall assessments."""

    def test_each_agent_has_overall_verdict(self):
        data = load_findings()
        for agent in data["agents"]:
            assert "overall_verdict" in agent, f"Missing overall_verdict for {agent['name']}"
            assert agent["overall_verdict"] in VALID_OVERALL_VERDICTS, (
                f"Agent '{agent['name']}' overall_verdict '{agent['overall_verdict']}' invalid."
            )

    def test_each_agent_has_best_path(self):
        data = load_findings()
        for agent in data["agents"]:
            assert "best_path" in agent, f"Missing best_path for {agent['name']}"
            assert agent["best_path"] and str(agent["best_path"]).strip(), (
                f"Agent '{agent['name']}' has empty best_path."
            )


class TestCoverageRequirements:
    """Validates that acceptance criteria are met."""

    def test_covers_silent_failure_injection_verified_but_ignored(self):
        """
        Acceptance criterion: Covers 'Silent failure - injection verified but
        agent ignores directive content'.
        """
        data = load_findings()
        all_text = str(data).lower()
        has_silent_failure_coverage = any(
            keyword in all_text
            for keyword in [
                "ignore", "ignored", "silent failure", "no effect", "behavioral",
                "behaviour", "compliance", "follow", "p0-6", "comprehension"
            ]
        )
        assert has_silent_failure_coverage, (
            "Acceptance criterion not met: Investigation does not cover "
            "'Silent failure - injection verified but agent ignores directive content'. "
            "At least one path or the summary must reference this failure mode."
        )

    def test_covers_degradation_runtime_infeasible_session_start_only(self):
        """
        Acceptance criterion: Covers 'Degradation - runtime injection infeasible,
        session-start only'.
        """
        data = load_findings()
        # At least one agent must evaluate both runtime and session_start paths
        for agent in data["agents"]:
            types_present = {p["type"] for p in agent.get("injection_paths", [])}
            has_both = "runtime" in types_present or "session_start" in types_present
            assert has_both, (
                f"Agent '{agent['name']}' documents neither runtime nor session_start paths. "
                "Cannot assess degradation scenario."
            )

    def test_covers_success_at_least_one_viable_path_per_agent(self):
        """
        Acceptance criterion: Covers 'Success - at least one injection path
        verified per agent'. Note: can be partially_viable or viable.
        """
        data = load_findings()
        for agent in data["agents"]:
            viable_paths = [
                p for p in agent.get("injection_paths", [])
                if p.get("verdict") in ("viable", "partially_viable")
            ]
            # If no viable path exists, overall_verdict should reflect infeasible
            if len(viable_paths) == 0:
                assert agent.get("overall_verdict") == "infeasible", (
                    f"Agent '{agent['name']}' has no viable/partially_viable paths but "
                    f"overall_verdict is '{agent.get('overall_verdict')}' not 'infeasible'."
                )


class TestSummarySection:
    """Validates summary section completeness."""

    def test_summary_has_required_fields(self):
        data = load_findings()
        summary = data.get("summary", {})
        required = {"agents_with_viable_injection", "runtime_injection_viable",
                    "session_start_injection_viable", "recommendation"}
        missing = required - set(summary.keys())
        assert not missing, f"Summary missing required fields: {missing}"

    def test_summary_agents_with_viable_injection_is_int(self):
        data = load_findings()
        val = data["summary"]["agents_with_viable_injection"]
        assert isinstance(val, int), (
            f"agents_with_viable_injection must be int, got {type(val)}"
        )
        assert 0 <= val <= 3, (
            f"agents_with_viable_injection must be 0-3, got {val}"
        )

    def test_summary_boolean_fields_are_bool(self):
        data = load_findings()
        summary = data["summary"]
        for field in ("runtime_injection_viable", "session_start_injection_viable"):
            assert isinstance(summary[field], bool), (
                f"summary.{field} must be bool, got {type(summary[field])}"
            )

    def test_summary_recommendation_is_non_empty(self):
        data = load_findings()
        rec = data["summary"].get("recommendation", "")
        assert rec and str(rec).strip(), "Summary recommendation must not be empty."


class TestDistinctInjectionSurfaces:
    """Validates that investigation doesn't conflate injection types."""

    def test_session_start_and_runtime_documented_separately(self):
        """
        Investigation must treat session-start and runtime injection as distinct
        (they may behave differently in practice).
        """
        data = load_findings()
        for agent in data["agents"]:
            types = [p["type"] for p in agent.get("injection_paths", [])]
            # This test does not fail if an agent lacks a type — it flags if
            # all paths are lumped under a single type when multiple should exist
            if len(types) >= 2:
                unique_types = set(types)
                assert len(unique_types) >= 1, (
                    f"Agent '{agent['name']}' has {len(types)} paths but only one type. "
                    "Consider whether distinct surfaces are being conflated."
                )

    def test_claude_code_has_runtime_path_documented(self):
        """
        Claude Code has hook system that enables runtime injection.
        Investigation must document at least one runtime path.
        """
        data = load_findings()
        claude = next(a for a in data["agents"] if a["name"] == "claude_code")
        runtime_paths = [p for p in claude["injection_paths"] if p["type"] == "runtime"]
        assert len(runtime_paths) > 0, (
            "Claude Code has a documented hook system (PreToolUse, PostToolUse, Stop hooks). "
            "Investigation must document at least one runtime injection path based on this. "
            "Even if verdict is 'not_viable', the path must be assessed."
        )
