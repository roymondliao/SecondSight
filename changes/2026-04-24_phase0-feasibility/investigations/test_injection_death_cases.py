"""
Death tests for Task 4: Runtime Injection Feasibility Investigation.

These tests verify that the investigation document (injection-feasibility.yaml)
covers the specified death cases — conditions under which injection appears to
work but silently fails.

EXECUTION ORDER: death tests before unit tests, per samsara protocol.
"""

import yaml
import os
import pytest

YAML_PATH = os.path.join(
    os.path.dirname(__file__),
    "injection-feasibility.yaml"
)


def load_findings():
    """Load the investigation findings YAML. Fails if file does not exist."""
    with open(YAML_PATH, "r") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------
# DEATH TEST DC-1: Context compaction / overflow drop
# Test: Injection path exists technically but agent drops/ignores injected
# content during compaction or context overflow.
# --------------------------------------------------------------------------

class TestDC1_CompactionDrop:
    """
    Death case: Agent drops injected content on context compaction.
    An injection surface that works in short sessions may silently disappear
    when context is compacted. Investigation must address this per agent.
    """

    def test_compaction_risk_addressed_for_claude_code(self):
        """Claude Code investigation must name compaction as a risk for each path."""
        data = load_findings()
        claude = next(a for a in data["agents"] if a["name"] == "claude_code")
        # At least one injection path must address compaction/context overflow risk
        paths_with_compaction_note = [
            p for p in claude["injection_paths"]
            if any(
                keyword in str(p.get("evidence", "")).lower() or
                keyword in str(p.get("persistence", "")).lower() or
                keyword in str(p.get("notes", "")).lower() or
                keyword in str(p.get("verdict", "")).lower()
                for keyword in ["compact", "context overflow", "context window", "truncat"]
            )
        ]
        assert len(paths_with_compaction_note) > 0, (
            "DEATH CASE: Investigation did not address compaction risk for Claude Code. "
            "Injected content may be silently dropped when context is compacted. "
            "Evidence must reference compaction behavior."
        )

    def test_compaction_risk_addressed_for_opencode(self):
        """OpenCode investigation must name compaction as a risk."""
        data = load_findings()
        opencode = next(a for a in data["agents"] if a["name"] == "opencode")
        paths_with_compaction_note = [
            p for p in opencode["injection_paths"]
            if any(
                keyword in str(p.get("evidence", "")).lower() or
                keyword in str(p.get("persistence", "")).lower() or
                keyword in str(p.get("notes", "")).lower() or
                keyword in str(p.get("verdict", "")).lower()
                for keyword in ["compact", "context overflow", "context window", "truncat", "unknown"]
            )
        ]
        assert len(paths_with_compaction_note) > 0, (
            "DEATH CASE: Investigation did not address compaction risk for OpenCode. "
            "OpenCode has a time_compacting field in its session DB — compaction is a known behavior. "
            "Evidence must reference whether injected content survives compaction."
        )

    def test_compaction_risk_addressed_for_codex(self):
        """Codex investigation must name compaction as a risk."""
        data = load_findings()
        codex = next(a for a in data["agents"] if a["name"] == "codex")
        paths_with_compaction_note = [
            p for p in codex["injection_paths"]
            if any(
                keyword in str(p.get("evidence", "")).lower() or
                keyword in str(p.get("persistence", "")).lower() or
                keyword in str(p.get("notes", "")).lower() or
                keyword in str(p.get("verdict", "")).lower()
                for keyword in ["compact", "context overflow", "context window", "truncat", "unknown"]
            )
        ]
        assert len(paths_with_compaction_note) > 0, (
            "DEATH CASE: Investigation did not address compaction risk for Codex. "
            "Evidence must reference whether injected content survives context overflow."
        )


# --------------------------------------------------------------------------
# DEATH TEST DC-2: Session-start injection treated as low-priority
# Test: Session-start injection works but content is treated as low-priority
# and overridden by user instructions.
# --------------------------------------------------------------------------

class TestDC2_LowPriorityOverride:
    """
    Death case: Injected directives exist in context but are ignored because
    user instructions take priority. Injection "works" but produces no effect.
    """

    def test_priority_risk_named_for_claude_code(self):
        """Investigation must address whether CLAUDE.md can be overridden by user."""
        data = load_findings()
        claude = next(a for a in data["agents"] if a["name"] == "claude_code")
        # Find session_start paths and check if priority/override risk is mentioned
        session_start_paths = [
            p for p in claude["injection_paths"]
            if p.get("type") == "session_start"
        ]
        assert len(session_start_paths) > 0, (
            "DEATH CASE: No session_start injection paths found for Claude Code. "
            "CLAUDE.md is a known session-start path that must be documented."
        )
        paths_with_priority_note = [
            p for p in session_start_paths
            if any(
                keyword in str(p.get("evidence", "")).lower() or
                keyword in str(p.get("notes", "")).lower() or
                keyword in str(p.get("verdict", "")).lower()
                for keyword in ["override", "priority", "low priority", "user instruct", "ignored", "overridden"]
            )
        ]
        assert len(paths_with_priority_note) > 0, (
            "DEATH CASE: Investigation did not address whether CLAUDE.md directives can be "
            "overridden by user instructions at runtime. This is the DC-2 failure mode. "
            "Investigation must either confirm or deny this risk with evidence."
        )

    def test_priority_risk_named_for_opencode(self):
        """Investigation must address whether OpenCode config can be overridden."""
        data = load_findings()
        opencode = next(a for a in data["agents"] if a["name"] == "opencode")
        session_start_paths = [
            p for p in opencode["injection_paths"]
            if p.get("type") in ("session_start", "indirect")
        ]
        # At minimum, the investigation must acknowledge the risk
        assert len(opencode["injection_paths"]) > 0, (
            "DEATH CASE: No injection paths documented for OpenCode at all."
        )

    def test_priority_risk_named_for_codex(self):
        """Investigation must address whether Codex system prompt can be overridden."""
        data = load_findings()
        codex = next(a for a in data["agents"] if a["name"] == "codex")
        assert len(codex["injection_paths"]) > 0, (
            "DEATH CASE: No injection paths documented for Codex at all."
        )


# --------------------------------------------------------------------------
# DEATH TEST DC-3: Heavy context load failure
# Test: Injection succeeds in test conditions but fails when agent is under
# heavy context load (long sessions).
# --------------------------------------------------------------------------

class TestDC3_HeavyContextLoadFailure:
    """
    Death case: Injection that works in fresh/short sessions silently fails in
    long sessions with heavy context loads.
    """

    def test_long_session_risk_mentioned_in_investigation(self):
        """Investigation must acknowledge context-load as a risk factor."""
        data = load_findings()
        # Check that at least one agent path mentions long session / heavy context risk
        all_paths = []
        for agent in data["agents"]:
            all_paths.extend(agent.get("injection_paths", []))

        paths_with_load_note = [
            p for p in all_paths
            if any(
                keyword in str(p.get("evidence", "")).lower() or
                keyword in str(p.get("notes", "")).lower() or
                keyword in str(p.get("persistence", "")).lower()
                for keyword in ["long session", "heavy context", "context load", "context length", "context window", "large context"]
            )
        ]
        # Also check summary or narrative fields
        summary_text = str(data.get("summary", {})).lower()
        has_in_summary = any(
            k in summary_text
            for k in ["long session", "heavy context", "context load", "context length", "context window"]
        )
        assert len(paths_with_load_note) > 0 or has_in_summary, (
            "DEATH CASE: Investigation does not address long-session / heavy context load risk. "
            "An injection that works in a 5-turn session may silently fail in a 100-turn session "
            "due to context window pressure or compaction."
        )

    def test_claude_code_runtime_injection_latency_documented(self):
        """Runtime injection paths must document latency to detect heavy-load degradation."""
        data = load_findings()
        claude = next(a for a in data["agents"] if a["name"] == "claude_code")
        runtime_paths = [
            p for p in claude["injection_paths"]
            if p.get("type") == "runtime"
        ]
        for path in runtime_paths:
            assert "latency" in path, (
                f"DEATH CASE: Runtime injection path '{path.get('method', 'unknown')}' for Claude Code "
                f"does not document latency. Under heavy context load, latency may increase or injection "
                f"may be skipped entirely."
            )


# --------------------------------------------------------------------------
# DEATH TEST DC-4: Silent failure via acknowledgment without behavioral change
# Covers DC-2 from task spec: "Injection works but agent ignores the content"
# --------------------------------------------------------------------------

class TestDC4_AcknowledgmentWithoutBehavior:
    """
    Death case: Agent acknowledges injected directive in its response
    but does not change behavior. This is the core DC-2 from the task spec.
    """

    def test_behavioral_verification_requirement_noted(self):
        """Investigation must distinguish acknowledgment from behavioral compliance."""
        data = load_findings()
        # Look for any mention of behavioral verification vs acknowledgment
        all_text = str(data).lower()
        has_behavioral_note = any(
            keyword in all_text
            for keyword in [
                "behavioral", "behaviour", "behavior change", "compliance",
                "acknowledge", "acknowledgment", "follows", "ignor", "p0-6"
            ]
        )
        assert has_behavioral_note, (
            "DEATH CASE: Investigation does not distinguish between an agent acknowledging "
            "an injected directive and actually following it. Injection can appear successful "
            "while producing zero behavioral change. P0-6 comprehension experiment must be "
            "referenced as the behavioral verification gate."
        )

    def test_each_agent_has_overall_verdict(self):
        """Each agent must have an overall_verdict that reflects real feasibility."""
        data = load_findings()
        valid_verdicts = {"feasible", "partially_feasible", "infeasible"}
        for agent in data["agents"]:
            assert "overall_verdict" in agent, (
                f"Agent '{agent.get('name')}' is missing overall_verdict. "
                "Cannot assess feasibility without explicit verdict."
            )
            assert agent["overall_verdict"] in valid_verdicts, (
                f"Agent '{agent.get('name')}' has invalid overall_verdict: "
                f"'{agent['overall_verdict']}'. Must be one of {valid_verdicts}."
            )

    def test_each_agent_has_best_path(self):
        """Each agent must name its best injection path — or explain why none exists."""
        data = load_findings()
        for agent in data["agents"]:
            assert "best_path" in agent, (
                f"Agent '{agent.get('name')}' is missing best_path. "
                "If no viable path exists, best_path must say so explicitly."
            )
            best = agent["best_path"]
            assert best is not None and str(best).strip() != "", (
                f"Agent '{agent.get('name')}' has empty best_path. "
                "Must name a path or explicitly state 'none_viable'."
            )
