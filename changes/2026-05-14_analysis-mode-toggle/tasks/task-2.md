# Task 2: AnalysisOutput pydantic contract (shared CLI/SDK schema)

## Context

Read: `overview.md`, `2-plan.md` §2.

This task introduces the single pydantic `BaseModel` that both CLI and SDK dispatchers must produce. It is the cross-mode contract — `intelligence.db` rows have one canonical shape regardless of which mode produced them. Without this contract, dual-mode dispatch silently drifts and DC2 / DC3 become impossible to detect.

The three output states (`success` / `failure` / `unknown`) are encoded via `status` field, not by ad-hoc nullability. Treating `unknown` as `failure` (or vice versa) is a defect — `unknown` means "outcome cannot be determined" and must be queryable as a corruption signature.

This task does NOT integrate with `intelligence.db` schema migration — only defines the in-memory contract. Storage integration is part of Task 6 (and may require DDL changes captured there).

## Files

- Create: `src/secondsight/analysis/output.py`
- Test: `tests/analysis/test_output_contract.py`

## Death Test Requirements

Before any implementation:

- Test: parsing JSON with missing `schema_version` → `ValidationError`
- Test: parsing JSON with `schema_version="2.0"` (future version) → `ValidationError` (only `"1.0"` accepted via `Literal`)
- Test: parsing JSON with `dispatched_via="cli"` but `cli_agent=None` → `ValidationError` (cross-field invariant: cli_agent required when dispatched_via=='cli')
- Test: parsing JSON with `dispatched_via="sdk"` but `primary_model=None` → `ValidationError` (cross-field invariant: primary_model required when dispatched_via=='sdk')
- Test: parsing JSON with `retry_count=-1` → `ValidationError` (must be `>= 0`)
- Test: parsing JSON with `retry_count=3` → `ValidationError` (must be `<= 2` per Decision #2: bounded retry)
- Test: parsing JSON with extra unknown field `"hallucination": "..."` → `ValidationError` (strict mode; `model_config = ConfigDict(extra="forbid")`)
- Test: `AnalysisOutput` instance can be JSON-serialized AND parsed back identical (round-trip).
- Test: `status="unknown"` instance has all required fields present (i.e., `unknown` is not a shortcut for "skip validation").
- Test: `behavior_flags=[]` is valid (DC3 — empty list is a valid shape but emits WARN downstream).

## Implementation Steps

- [ ] Step 1: Write death tests above — they should fail on import
- [ ] Step 2: Run death tests — verify import failure
- [ ] Step 3: Write happy-path unit tests (construct AnalysisOutput with all fields, verify field access)
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement `src/secondsight/analysis/output.py`:
  - `BehaviorFlag` sub-model (re-use existing if present in `analysis/behavior.py` types; else define here)
  - `SessionSummary` sub-model (re-use existing if present; else define here)
  - `AnalysisStatus = Literal["success", "failure", "unknown"]`
  - `AnalysisOutput` with `schema_version: Literal["1.0"]`, `session_id`, `status`, `behavior_flags`, `session_summary`, `dispatched_via: Literal["cli", "sdk"]`, `cli_agent: str | None`, `primary_model: str | None`, `fallback_used: bool = False`, `retry_count: int = 0` (with `ge=0, le=2`), `error_details: dict | None = None`
  - `model_config = ConfigDict(extra="forbid", frozen=True)`
  - Field validators for cross-field invariants
- [ ] Step 6: Run all tests — verify pass
- [ ] Step 7: Generate JSON schema (`AnalysisOutput.model_json_schema()`) — this string will be embedded in jinja prompts (Task 3) so CLI prompts can instruct coding agents on the required output shape
- [ ] Step 8: Run `pre-commit run --all-files`
- [ ] Step 9: Write scar report
- [ ] Step 10: Commit

## Expected Scar Report Items

- Potential shortcut: making `cli_agent` and `primary_model` both default to `None` without enforcing cross-field invariant. **Don't.** Without the invariant, downstream queries can't trust the field meaningfully.
- Potential shortcut: allowing `status="unknown"` with `behavior_flags` populated. Decide whether unknown should forbid populated fields or allow them as best-effort partial data. Record the decision.
- Assumption to verify: existing `BehaviorFlag` / `SessionSummary` types in `src/secondsight/analysis/behavior.py` and adjacent files — re-use those if compatible (don't duplicate the type tree). If they're stdlib `@dataclass`, may need conversion or pydantic wrappers.
- Assumption to verify: `pydantic` v2 in `pyproject.toml`. v1-style `Config` class is wrong; v2 uses `model_config = ConfigDict(...)`.
- Watch for: `extra="forbid"` strictness — when SDK mode produces output via `pydantic-ai`, pydantic-ai may add metadata fields. Verify that `pydantic-ai` does NOT inject fields into the output type.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DC2 (schema mismatch detection — validation infrastructure for it)
- DC3 (empty behavior_flags valid shape)
- DC4 partial (`error_details` field carries provider_errors)
- All "evidence chain" requirements in happy_path scenarios (status, dispatched_via, retry_count fields)
