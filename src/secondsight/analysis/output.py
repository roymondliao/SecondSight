"""AnalysisOutput pydantic contract — cross-mode shared schema (Task 2).

Single source of truth for the shape that both CLI and SDK dispatchers must
produce. `intelligence.db` rows have one canonical shape regardless of which
mode produced them.

Design decisions recorded here:
1. Three output states (`success` / `failure` / `unknown`) — `unknown` is NOT
   coalesced to `failure`. `unknown` means "outcome cannot be determined" and
   is queryable as a corruption signature (DC2/DC3/DC4 per 2-plan.md §2).

2. Cross-field invariants (enforced via @model_validator):
   - dispatched_via='cli'  → cli_agent is required, primary_model must be None
   - dispatched_via='sdk'  → primary_model is required, cli_agent must be None
   This is "exactly one is populated, never both, never neither."

3. unknown status + field invariants (Decision A from task spec):
   Even in `unknown` state, dispatched_via tells us which mode was attempted
   and the corresponding identity field (cli_agent or primary_model) tells us
   against what resource. This is forensically more valuable than allowing
   nulls. The dispatcher must know what it was about to invoke even if it
   couldn't complete.

4. behavior_flags uses BehaviorFlagDraft from analysis.schemas — the exact LLM-
   emittable subset (flag_type, event_ids, reason, confidence). The orchestrator
   promotes Draft → BehaviorFlag by injecting persistence fields (id, project_id,
   session_id, segment_index, created_at). AnalysisOutput carries Drafts because
   at dispatch time those persistence fields do not yet exist.
   output.py already imports BehaviorFlagType from schemas.py; adding BehaviorFlagDraft
   from the same module carries no circular import risk.

5. frozen=True enforces immutability: once parsed, the output contract is sealed.
   This prevents callers from mutating results after validation passes.

   # Migration note: when rotating schema_version to "2.0", add a versioned parser
   # + provide intelligence.db migration for rows stored under "1.0". Do not
   # silently coerce versions in this validator — surface them as parse failures.

   # model_construct() bypass: model_construct() skips all pydantic validation,
   # including cross-field invariants and DC4 checks. Use AnalysisOutput.model_validate(...)
   # for any untrusted or externally-sourced input (e.g., re-parsing a DB row).
   # Call-site discipline is required; no model-level guard can close this gap.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from secondsight.analysis.schemas import BehaviorFlagDraft
from secondsight.config.constants import BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP

# ---------- Type alias ----------

AnalysisStatus = Literal["success", "failure", "unknown"]


# ---------- Sub-models ----------


class SessionSummary(BaseModel):
    """Structured summary of an analyzed session.

    This is the output shape for session-level summary data in AnalysisOutput.
    Kept separate from `analysis.schemas.SessionReport` (which is the DB row
    shape including id, project_id, etc.). This type holds only the content
    fields that the LLM produces.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str
    key_findings: list[str] = Field(default_factory=list)
    body: str


# ---------- Main contract ----------


class AnalysisOutput(BaseModel):
    """Cross-mode output contract for dual-mode analysis dispatch.

    Both CLI and SDK dispatchers MUST produce an instance of this model.
    Intelligence.db rows have one canonical shape regardless of mode.

    Fields:
        schema_version: Literal "1.0" — future schema changes bump this.
            Only "1.0" is accepted; "2.0" or any other value is rejected
            at parse time (DC2).
        session_id: The session that was analyzed.
        status: Three-state outcome (success/failure/unknown). See module
            docstring for semantics.
        behavior_flags: Zero or more detected behavior flags. Empty list is
            a valid shape (DC3 — downstream WARNS if >N events but flags=[]).
            Carries BehaviorFlagDraft (LLM-emittable shape); the orchestrator
            promotes to BehaviorFlag by injecting persistence fields.
        session_summary: Structured summary of the analyzed session.
        dispatched_via: Which mode produced this output. Telemetry field;
            enables querying success rate per mode.
        cli_agent: Populated ONLY if dispatched_via=='cli'. None for sdk.
            Cross-field invariant enforced by @model_validator.
        primary_model: Populated ONLY if dispatched_via=='sdk'. None for cli.
            Cross-field invariant enforced by @model_validator.
        fallback_used: SDK fallback engaged? Only meaningful when
            dispatched_via=='sdk'. Defaults False (always False for cli).
        retry_count: How many parse-retries happened. Bounded [0, 5] by the
            Phase 1 global hard cap. Runtime policy may choose any value within
            that bound, but the output contract will not accept a larger count.
        error_details: On failure status, carries error information.
            For DC4 (SDK both providers fail), MUST carry BOTH errors:
            {"primary_error": "...", "fallback_error": "..."}.
            Enforced by @model_validator when fallback_used=True.
            None for success outcomes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    session_id: str
    status: AnalysisStatus
    behavior_flags: list[BehaviorFlagDraft]
    session_summary: SessionSummary
    dispatched_via: Literal["cli", "sdk"]
    cli_agent: str | None = None
    primary_model: str | None = None
    fallback_used: bool = False
    retry_count: int = Field(default=0, ge=0, le=BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP)
    error_details: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_cross_fields(self) -> "AnalysisOutput":
        """Enforce dispatched_via ↔ cli_agent/primary_model cross-field invariants
        and DC4 error_details completeness requirement.

        Rules:
        - dispatched_via='cli'  → cli_agent required, primary_model must be None
        - dispatched_via='sdk'  → primary_model required, cli_agent must be None
        - Never both populated, never neither (for the matching field)
        - DC4: status='failure' + dispatched_via='sdk' + fallback_used=True →
          error_details must be present AND contain both 'primary_error' and
          'fallback_error' keys.

        This holds for ALL status values including 'unknown' (Decision A):
        even in unknown state, we know which mode was attempted and against
        what resource. Forensic value outweighs the convenience of allowing
        null identity fields in unknown state.

        # NOTE: when adding a third dispatched_via value (e.g. "hosted"), add
        # a branch here for its identity-field invariants.
        # The Literal type at the field definition is the type-level bet;
        # this validator is the behavior-level bet. Both must be updated together.
        """
        if self.dispatched_via == "cli" and self.cli_agent is None:
            raise ValueError("cli_agent is required when dispatched_via='cli'")
        if self.dispatched_via == "sdk" and self.primary_model is None:
            raise ValueError("primary_model is required when dispatched_via='sdk'")
        if self.dispatched_via == "cli" and self.primary_model is not None:
            raise ValueError(
                "primary_model must be None when dispatched_via='cli'; "
                "only cli_agent should be populated"
            )
        if self.dispatched_via == "sdk" and self.cli_agent is not None:
            raise ValueError(
                "cli_agent must be None when dispatched_via='sdk'; "
                "only primary_model should be populated"
            )

        # DC4: SDK dual-failure MUST carry both error strings.
        # Cross-field conditional: JSON schema cannot express this constraint
        # (standard JSON schema has no conditional field-requirement mechanism
        # tied to other field values). Runtime enforcement here is the only gate.
        if self.status == "failure" and self.dispatched_via == "sdk" and self.fallback_used is True:
            if self.error_details is None:
                raise ValueError(
                    "DC4: SDK dual-failure (fallback_used=True) MUST carry error_details "
                    "with both 'primary_error' and 'fallback_error' keys. Got None."
                )
            if "primary_error" not in self.error_details:
                raise ValueError(
                    "DC4: error_details is missing required key 'primary_error'. "
                    "SDK dual-failure MUST document both provider errors."
                )
            if "fallback_error" not in self.error_details:
                raise ValueError(
                    "DC4: error_details is missing required key 'fallback_error'. "
                    "SDK dual-failure MUST document both provider errors."
                )

        return self


__all__ = [
    "AnalysisOutput",
    "AnalysisStatus",
    "BehaviorFlagDraft",
    "SessionSummary",
]
