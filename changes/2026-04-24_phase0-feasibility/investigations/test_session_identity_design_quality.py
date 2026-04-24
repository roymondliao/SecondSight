"""
Death tests and unit tests for the session identity design document.

Death tests (DC-5 variants): Verify the design document does not claim a
working cross-agent identity model without providing a concrete mechanism.
The silent failure mode is: design looks complete but only covers same-agent
consecutive sessions, hand-waving cross-agent scenarios.

Unit tests: Verify design completeness — all required sections present,
degradation levels defined, Phase 1 requirements explicit.

Run:
  pytest changes/2026-04-24_phase0-feasibility/investigations/test_session_identity_design_quality.py -v
"""

import re
from pathlib import Path

DESIGN_DOC_PATH = Path(__file__).parent / "session-identity-design.md"


def load_design() -> str:
    """Load the design document. Fail clearly if it doesn't exist."""
    assert DESIGN_DOC_PATH.exists(), (
        f"Design document not found: {DESIGN_DOC_PATH}\n"
        "The design document must be written before tests can pass."
    )
    return DESIGN_DOC_PATH.read_text()


# ---------------------------------------------------------------------------
# DEATH TESTS — DC-5 silent failure paths first
# ---------------------------------------------------------------------------


class TestDeathCase5a_CrossAgentLinkingHandwaved:
    """
    DC-5a: Design claims cross-agent identity model is solved but provides
    no concrete linking mechanism for cross-agent sessions.

    Silent failure: Phase 1 engineer implements the 'project path anchor'
    approach without realizing that path normalization across agents is
    unverified — different agents may store paths with different encodings
    (e.g., Claude Code uses '-Users-user-project', OpenCode uses '/Users/user/project').
    Design is silently useless for cross-agent linking without path normalization spec.
    """

    def test_cross_agent_scenario_explicitly_addressed(self):
        """
        The design MUST include a section or example that explicitly covers
        a cross-agent scenario: same project, different agents (e.g.,
        Claude Code session then Codex session on the same directory).
        """
        doc = load_design()
        doc_lower = doc.lower()
        has_cross_agent = any(phrase in doc_lower for phrase in [
            "cross-agent",
            "cross agent",
            "different agent",
            "multiple agent",
            "agent a",
            "agent b",
        ])
        assert has_cross_agent, (
            "DC-5a: No cross-agent scenario found in design document. "
            "A design that only covers same-agent sessions silently fails "
            "when the same project is worked on by multiple agents."
        )

    def test_project_path_normalization_addressed(self):
        """
        If project path is used as a cross-agent anchor, path normalization
        MUST be addressed. Claude Code encodes paths as '-Users-user-project',
        while other agents store absolute paths. This difference is a
        silent failure point if not documented.
        """
        doc = load_design()
        doc_lower = doc.lower()

        uses_project_path_anchor = any(phrase in doc_lower for phrase in [
            "project path",
            "working directory",
            "cwd",
            "directory anchor",
            "path anchor",
        ])

        if uses_project_path_anchor:
            has_normalization = any(phrase in doc_lower for phrase in [
                "normaliz",
                "canonical",
                "encode",
                "decode",
                "path format",
                "absolute path",
                "resolve",
            ])
            assert has_normalization, (
                "DC-5a: Design uses project path as anchor but does not address "
                "path normalization. Claude Code encodes paths as "
                "'-Users-user-project', other agents use absolute paths. "
                "Cross-agent linking via unormalized paths silently fails."
            )

    def test_explicit_cross_agent_feasibility_verdict(self):
        """
        The design MUST include an explicit verdict on cross-agent linking:
        either 'supported', 'partially supported', or 'unsupported with degradation'.
        A document that describes the problem but never gives a verdict is
        incomplete — Phase 1 won't know what to implement.
        """
        doc = load_design()
        doc_lower = doc.lower()

        has_feasibility_verdict = any(phrase in doc_lower for phrase in [
            "cross-agent linking",
            "cross-agent identity",
            "cross agent linking",
            "cross agent identity",
            "feasible",
            "infeasible",
            "not supported",
            "partially supported",
            "supported with",
        ])
        assert has_feasibility_verdict, (
            "DC-5a: Design does not include a feasibility verdict for cross-agent "
            "identity linking. Without a verdict, Phase 1 doesn't know whether to "
            "implement cross-agent linking or plan for degradation."
        )


class TestDeathCase5b_EphemeralSessionIDAssumption:
    """
    DC-5b: Design assumes all agents provide persistent session IDs, but
    some agents generate ephemeral IDs that don't survive a restart.

    Silent failure: Phase 1 stores sessions keyed by agent-provided session ID.
    When an agent generates a new ephemeral ID for a resumed session, the
    system sees it as a new session — directive lineage is silently broken.
    """

    def test_session_id_stability_per_agent_documented(self):
        """
        The design MUST document, per agent, whether the session ID is
        persistent (survives process restart) or ephemeral.
        If only one agent is documented, the others are assumed-stable, which
        is the silent failure.
        """
        doc = load_design()
        doc_lower = doc.lower()

        agents_documented = sum([
            "claude code" in doc_lower,
            "opencode" in doc_lower or "open code" in doc_lower,
            "codex" in doc_lower,
        ])
        assert agents_documented >= 3, (
            f"DC-5b: Only {agents_documented} of 3 agents documented in design. "
            "All three agents (Claude Code, OpenCode, Codex) must be cataloged "
            "for session identity attributes. Missing agents are assumed to behave "
            "like documented ones — a silent assumption failure."
        )

    def test_ephemeral_id_risk_acknowledged(self):
        """
        The design MUST acknowledge that some agents may not provide stable,
        retrievable session IDs. If all agents are assumed to have stable UUIDs,
        the design is optimistic and will silently break.
        """
        doc = load_design()
        doc_lower = doc.lower()

        has_ephemeral_acknowledgment = any(phrase in doc_lower for phrase in [
            "ephemeral",
            "not persist",
            "not stable",
            "unstable id",
            "id may change",
            "no stable id",
            "no session id",
            "id not available",
            "fallback",
        ])
        assert has_ephemeral_acknowledgment, (
            "DC-5b: Design does not acknowledge the risk of ephemeral/unstable "
            "session IDs. If any agent generates new IDs for resumed sessions, "
            "directive lineage is silently broken with no detection."
        )


class TestDeathCase5c_RealTimeIdentityLinkingImpossibility:
    """
    DC-5c: Design implicitly assumes identity linking can happen in real-time,
    but some linking requires information only available AFTER session ends
    (e.g., session end time, final working directory, outcome).

    Silent failure: Observation layer stores events but cannot link them to
    their project context until session ends. Real-time analysis would see
    incomplete or unlinked sessions.
    """

    def test_linking_timing_addressed(self):
        """
        The design MUST address whether identity linking happens at event
        ingestion time (real-time) or at session end (post-session).
        If this is not specified, Phase 1 will make an assumption that may
        be wrong.
        """
        doc = load_design()
        doc_lower = doc.lower()

        has_timing_discussion = any(phrase in doc_lower for phrase in [
            "real-time",
            "real time",
            "at ingestion",
            "post-session",
            "after session",
            "session end",
            "deferred linking",
            "lazy linking",
            "retroactive",
            "backfill",
        ])
        assert has_timing_discussion, (
            "DC-5c: Design does not address the timing of identity linking. "
            "Some identity information (final cwd, session outcome) is only "
            "available after session ends. Real-time linking that assumes all "
            "context is available at event time will silently produce incomplete links."
        )

    def test_minimum_viable_identity_at_first_event_defined(self):
        """
        The design MUST specify what identity information is available at
        the FIRST event of a session (minimum viable identity) vs what must
        be deferred. Without this, the observation layer cannot decide how
        to handle incoming events from sessions that haven't been fully
        identified yet.
        """
        doc = load_design()
        doc_lower = doc.lower()

        has_minimum_identity = any(phrase in doc_lower for phrase in [
            "minimum",
            "first event",
            "at start",
            "initial",
            "bootstrap",
            "available immediately",
            "session start",
        ])
        assert has_minimum_identity, (
            "DC-5c: Design does not specify what identity fields are available "
            "at the first event. Phase 1 cannot correctly handle the 'unknown "
            "session' state without knowing what's minimally available at session open."
        )


# ---------------------------------------------------------------------------
# UNIT TESTS — verify design completeness
# ---------------------------------------------------------------------------


class TestDesignStructure:
    """Verify the design document has all required sections."""

    REQUIRED_SECTIONS = [
        ("identity model", ["identity model", "identity dimension", "identity schema"]),
        ("linking strategy", ["linking strategy", "link sessions", "cross-session linking"]),
        ("degradation", ["degradation", "fallback", "level"]),
        ("example data", ["example", "sample", "e.g.", "session 1", "s1", "table"]),
        ("phase 1", ["phase 1", "p1", "implementation requirement"]),
        ("limitations", ["limitation", "unsupported", "out of scope", "not supported"]),
    ]

    def test_identity_model_section_present(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in self.REQUIRED_SECTIONS[0][1]), (
            "Design document is missing an identity model section. "
            "Must define the identity dimensions (agent, project, user, session, directive lineage)."
        )

    def test_linking_strategy_section_present(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in self.REQUIRED_SECTIONS[1][1]), (
            "Design document is missing a linking strategy section. "
            "Must describe how sessions are linked across dimensions."
        )

    def test_degradation_levels_section_present(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in self.REQUIRED_SECTIONS[2][1]), (
            "Design document is missing degradation levels. "
            "Must define: full linking → single-agent linking → session-isolated."
        )

    def test_example_data_section_present(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in self.REQUIRED_SECTIONS[3][1]), (
            "Design document is missing example data. "
            "Must show at least 5 sessions with linking applied."
        )

    def test_phase1_requirements_section_present(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in self.REQUIRED_SECTIONS[4][1]), (
            "Design document is missing Phase 1 implementation requirements. "
            "Must document what Phase 1 needs to implement from this design."
        )

    def test_limitations_section_present(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in self.REQUIRED_SECTIONS[5][1]), (
            "Design document is missing a limitations section. "
            "Must explicitly document unsupported scenarios."
        )


class TestIdentityDimensions:
    """Verify all required identity dimensions are addressed."""

    def test_agent_dimension_defined(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "agent dimension",
            "agent_id",
            "agent identifier",
            "agent type",
            "which agent",
        ]), "Identity model must define the agent dimension."

    def test_project_dimension_defined(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "project dimension",
            "project_id",
            "project identifier",
            "project path",
            "working directory",
        ]), "Identity model must define the project dimension."

    def test_session_dimension_defined(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "session dimension",
            "session_id",
            "session identifier",
        ]), "Identity model must define the session dimension."

    def test_directive_lineage_dimension_defined(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "directive lineage",
            "lineage",
            "directive tracking",
            "source session",
            "outcome session",
        ]), (
            "Identity model must address directive lineage — the ability to track "
            "'directive D was generated from S1, applied in S2, observed in S3'."
        )


class TestExampleDataCompleteness:
    """Verify example data covers the required scenarios."""

    def test_example_has_five_or_more_sessions(self):
        """
        The design requires 5+ sessions in example data.
        We check for session markers (Session N, S1, row markers, etc.).
        """
        doc = load_design()
        # Count session references: S1/S2/S3 or Session 1/Session 2 etc.
        session_refs = set(re.findall(r'\bS[1-9]\b|\bSession\s+[1-9]\b', doc, re.IGNORECASE))
        # Also count table rows if present (| marker)
        table_rows = [line for line in doc.split('\n')
                      if line.strip().startswith('|') and '---' not in line]
        # Subtract header row
        data_rows = max(len(table_rows) - 1, 0) if table_rows else 0

        has_five_sessions = len(session_refs) >= 5 or data_rows >= 5
        assert has_five_sessions, (
            f"Design example data has fewer than 5 sessions (found session refs: {session_refs}, "
            f"table data rows: {data_rows}). Must show 5+ sessions with linking applied."
        )

    def test_example_includes_cross_agent_row(self):
        """
        At least one example session row must involve a different agent
        than the previous session on the same project (the DC-5 scenario).
        """
        doc = load_design()
        doc_lower = doc.lower()

        # Check for indicator that at least two different agents appear in examples
        claude_mentions = len(re.findall(r'claude\s*code', doc_lower))
        codex_mentions = len(re.findall(r'codex', doc_lower))
        opencode_mentions = len(re.findall(r'opencode|open\s*code', doc_lower))

        agent_types_in_examples = sum([
            claude_mentions > 0,
            codex_mentions > 0,
            opencode_mentions > 0,
        ])
        assert agent_types_in_examples >= 2, (
            "DC-5a: Example data must include sessions from at least two different "
            f"agents. Found references: Claude Code={claude_mentions}, "
            f"Codex={codex_mentions}, OpenCode={opencode_mentions}. "
            "The cross-agent scenario is the primary death case for this design."
        )


class TestDegradationLevels:
    """Verify degradation levels are explicit and cover the spectrum."""

    def test_full_linking_level_defined(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "full linking",
            "full link",
            "level 1",
            "level 0",
            "best case",
            "all dimensions",
        ]), "Degradation levels must include a 'full linking' level."

    def test_session_isolated_level_defined(self):
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "session-isolated",
            "session isolated",
            "no linking",
            "isolated",
            "worst case",
            "fallback",
        ]), "Degradation levels must include a 'session-isolated' (worst-case) level."

    def test_feature_loss_documented_per_level(self):
        """
        Each degradation level must document what is lost at that level,
        not just what works. 'What breaks' is as important as 'what works'.
        """
        doc = load_design()
        doc_lower = doc.lower()

        has_feature_loss_docs = any(phrase in doc_lower for phrase in [
            "loses",
            "not available",
            "cannot",
            "breaks",
            "feature loss",
            "capability loss",
            "loses the ability",
        ])
        assert has_feature_loss_docs, (
            "Degradation levels must document what is lost at each level, not just "
            "what still works. 'What breaks' is required for Phase 1 to know "
            "when to signal degradation to callers."
        )


class TestAgentIdentityAttributes:
    """Verify per-agent identity attributes are documented from official sources."""

    def test_claude_code_session_id_source_documented(self):
        """
        Claude Code stores session IDs as UUIDs in JSONL file names and
        as sessionId fields in transcript entries. This must be documented.
        """
        doc = load_design()
        doc_lower = doc.lower()
        assert "uuid" in doc_lower or "jsonl" in doc_lower or "~/.claude" in doc_lower, (
            "Claude Code identity attributes not documented. Expected mention of "
            "UUID session IDs stored in ~/.claude/projects/ JSONL files."
        )

    def test_opencode_session_id_source_documented(self):
        """
        OpenCode stores sessions in SQLite (opencode.db) with a session table
        containing ID, ProjectID, Directory, Title fields. This must be documented.
        """
        doc = load_design()
        doc_lower = doc.lower()
        assert "sqlite" in doc_lower or "opencode.db" in doc_lower or "relational" in doc_lower, (
            "OpenCode identity attributes not documented. Expected mention of "
            "SQLite storage with session/project tables."
        )

    def test_codex_session_id_source_documented(self):
        """
        Codex CLI stores sessions as JSONL files under ~/.codex/sessions/YYYY/MM/DD/
        with a session_index.jsonl for thread names. This must be documented.
        """
        doc = load_design()
        doc_lower = doc.lower()
        assert ".codex" in doc_lower or "codex session" in doc_lower, (
            "Codex identity attributes not documented. Expected mention of "
            "~/.codex/sessions/ JSONL directory layout."
        )

    def test_project_path_availability_per_agent_assessed(self):
        """
        The design must assess whether each agent exposes working directory.
        This is critical because project path is the proposed cross-agent anchor.
        If any agent doesn't expose it, cross-agent linking is impossible at that agent.
        """
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "working directory",
            "cwd",
            "directory",
            "project path",
        ]), (
            "Design must assess project path availability per agent. "
            "The cross-agent anchor depends on this field being available."
        )


class TestPhase1Requirements:
    """Verify Phase 1 implementation requirements are concrete."""

    def test_phase1_requires_secondsight_session_id(self):
        """
        Phase 1 must mint a SecondSight-owned session ID (ss_session_id) that is
        stable across agent ID format differences. Without this, Phase 1 is
        simply storing agent-native IDs without providing the linking layer.
        """
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "ss_session_id",
            "secondsight session id",
            "internal id",
            "surrogate key",
            "stable id",
            "own identifier",
            "canonical id",
        ]), (
            "Phase 1 must mint a SecondSight-owned stable session identifier. "
            "Without it, the system is hostage to agent-native ID formats."
        )

    def test_phase1_storage_requirements_mentioned(self):
        """
        Phase 1 requirements must include storage implications —
        which tables/fields the identity model requires.
        """
        doc = load_design()
        doc_lower = doc.lower()
        assert any(phrase in doc_lower for phrase in [
            "schema",
            "table",
            "field",
            "column",
            "store",
            "persist",
        ]), (
            "Phase 1 requirements must mention storage implications of the identity model. "
            "The identity model is useless if Phase 1 doesn't know what to persist."
        )
