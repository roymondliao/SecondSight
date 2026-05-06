# Security & Privacy Review — GUR-100 Bundle

**Verdict:** PASS — 0 HIGH, 0 MEDIUM findings, 0 risks accepted.

**Reviewed:** bundle commit `d58b64b` (GUR-100 Phase 2 Analysis Core
implementation). Phase 1 surface is out of scope — prior security
reviews shipped under `Security & privacy review: GUR-96 bundle`
(commit 553a739) and `Security & privacy review: GUR-97 bundle`
(commit a170aea).

## Scope (in-scope production files)

- `src/secondsight/analysis/__init__.py`
- `src/secondsight/analysis/schemas.py`
- `src/secondsight/analysis/segmenter.py`
- `src/secondsight/analysis/metrics.py`
- `src/secondsight/storage/behavior_flags_table.py`
- `src/secondsight/storage/behavior_flags_repository.py`
- `src/secondsight/storage/directives_table.py`
- `src/secondsight/storage/directives_repository.py`
- `src/secondsight/storage/__init__.py` (re-exports only)
- `docs/system_design.md` (§5.5.2 + §7.4 patches — markdown only)
- `tests/analysis/test_{schemas,segmenter,metrics}.py`
- `tests/storage/test_{behavior_flags,directives}_repository.py`

## Method

Platform-built-in security review (security-review skill via samsara
review pipeline) executed on the bundle diff vs. base. Sub-task
identification + filtering against HARD EXCLUSIONS and PRECEDENTS;
confidence threshold ≥ 8/10 for any reported finding.

## Findings

**No HIGH or MEDIUM confidence findings.**

## Threat surface verification

The bundle's threat surface is intentionally narrow:

- **No new HTTP endpoints** — exposure is GUR-104's scope, not this bundle.
- **No LLM calls** — analysis prompts are GUR-101's scope.
- **No subprocess invocations.**
- **No filesystem writes outside SQLAlchemy's DB engine.**
- **No new external network paths.**

Verified safety properties:

- **Parameterized SQL throughout.** Every query in the new repositories
  uses SQLAlchemy's parameterized API:
  `sqlite_insert(table).values(**row).on_conflict_do_nothing(...)`,
  `sa.select(table).where(col == param)`,
  `sa.update(table).where(...).values(**values)`. No `text()` calls,
  no string concatenation, no f-string SQL.
- **JSON-encoded TEXT columns** (`event_ids: list[str]` and
  `source_sessions: list[str]`) round-trip via `json.dumps` /
  `json.loads`. The `json` module does not evaluate code; a tampered
  DB row at worst raises `json.JSONDecodeError` or yields a non-list
  that downstream Pydantic validation rejects.
- **Enum-bypass defense (D1 contract).** Pydantic v2's
  `model_construct()` skips field validators. The repositories'
  defensive `_guard()` methods re-validate on insert:
  `BehaviorFlagsRepository._guard` checks `confidence ∈
  {high,medium,low}` and `flag_type ∈ BehaviorFlagType`;
  `DirectivesRepository._guard` checks `status ∈ DirectiveStatus`,
  `type ∈ DirectiveType`, and the lifecycle invariant
  (`disabled_at`/`disabled_reason` non-None iff `status=DISABLED`).
- **Logging honesty: identifiers only, never content.**
  `_logger.warning(...)` calls in segmenter and metrics include
  `event_id`, `sequence_number`, `session_id`, `event_type`,
  `tool_name`, `project_id` — internal identifiers. The bundle
  deliberately does NOT log `event.data`, `disabled_reason`,
  `instruction`, or `intent_summary` text fields. PII surface is
  contained.
- **Test code** carries no hardcoded credentials or secrets.
- **SD doc patches** are pure markdown (no executable surface).

## Risks accepted

None.

## Carry-forward

Items deferred to other gates (not security-relevant; recorded for
ship-manifest visibility):

- **Empty-string `reason` accepted for DISABLED transitions** — the
  current contract only rejects `reason is None`; an empty string
  passes. Audit-trail concern, not security; deferred to GUR-104's
  HTTP layer (per `project_directive_lifecycle_contract`).
- **Raw `event.data` passthrough in `SegmentData`** — the segmenter's
  `_event_to_dict` includes `event.data` as-is. This is by design (the
  analysis LLM in GUR-101 needs the data). The privacy boundary is
  enforced at the LLM call site (GUR-101), not here. Phase 1's
  raw-trace policy still applies.
- **`disabled_reason` text field stores arbitrary caller input** —
  could contain anything (PII, secrets) depending on caller. Same
  privacy posture as `events.data`; documented behavior.
