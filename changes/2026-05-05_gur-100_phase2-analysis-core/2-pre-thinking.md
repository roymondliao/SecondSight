# Planning Pre-thinking — Information Assumptions

> Required gate before writing `2-plan.md` per samsara `planning` skill.
> Identifies what I am about to assume to write the plan, and where my
> assumptions diverge from what Research established.

## To write this plan, I am assuming

- **A1 — Module location:** new code lives under `src/secondsight/analysis/`,
  mirroring the layout of existing peer packages (`api/`, `observation/`,
  `storage/`, `cli/`, `installer/`). The package gets its own `__init__.py`
  + `schemas.py` + `segmenter.py` + `metrics.py` plus two new files
  `behavior_flags_table.py` / `behavior_flags_repository.py` and
  `directives_table.py` / `directives_repository.py` (or a single
  `tables.py` file — decided in the plan).
  *Confirmed by reading existing src layout.*
- **A2 — Test layer location:** tests live under `tests/analysis/`,
  mirroring `tests/storage/`, `tests/observation/`, etc. Each module
  gets its own test file. *Confirmed by reading the existing
  `tests/` tree convention.*
- **A3 — Existing repository pattern:** `events_repository.py` is the
  reference implementation. New repositories use the same pattern:
  injected `DBEngine`, `metadata.create_all(checkfirst=True)` for
  idempotent schema creation, `INSERT … ON CONFLICT(id) DO NOTHING`
  for idempotency on primary key. *Confirmed by reading
  `src/secondsight/storage/events_repository.py`.*
- **A4 — No DB CHECK constraints on enum-shaped TEXT columns.**
  `events.event_type` is plain TEXT with no CHECK; validation lives at
  the Python layer (Pydantic on construction + repository defensive check).
  GUR-100 follows the same convention: `behavior_flags.flag_type`,
  `directives.status`, `directives.type` are TEXT with Python-layer
  validation, no CHECK constraint. *Confirmed by reading
  `src/secondsight/storage/events_table.py`.*
- **A5 — Pydantic model shape feeds both DB persistence and LLM-output
  validation.** `BehaviorFlag` (in `analysis/schemas.py`) is a single
  model used by:
  (a) the repository on insert (validates flag_type + confidence enums),
  (b) GUR-101's prompt-output parser (validates that the LLM produced
  one of the 6 enum values + a valid confidence label).
  Repository handles `event_ids: list[str]` ↔ JSON-encoded TEXT
  round-trip on the way to/from the DB.
- **A6 — `SegmentData` is a Pydantic model in `analysis/schemas.py`.**
  Same rationale as A5 — it's the shape the LLM prompt builder
  consumes (GUR-101). Defining it in `schemas.py` makes the contract
  visible to both the segmenter (producer) and the prompt builder
  (consumer).
- **A7 — Logging convention:** existing modules use `logging.getLogger(__name__)`.
  GUR-100 follows the same pattern; null-token-count warnings emit at
  WARNING level with a structured prefix. *Confirmed by reading
  `src/secondsight/observation/tracker.py`.*
- **A8 — Tool-name vocabulary for `unique_files` metric.** SD §5.3.1
  doesn't enumerate the file-touching tool set. Default proposal:
  `{"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"}` (Claude Code
  tool surface). Configurable via a module-level constant; not a
  function argument (the metric should be deterministic per fixture).
- **A9 — Assignment of issue-numbering inside the plan.** Wake-context
  task numbering is P2-1..P2-4. I'll renumber the in-repo tasks
  task-1..task-5 to factor out `analysis/__init__.py` + `schemas.py`
  as task-1 (scaffold + enum + Pydantic models — the contract anchor
  every other task depends on). The original P2-1 (behavior flags table)
  becomes task-2; original P2-2 becomes task-3; P2-3 → task-4; P2-4 →
  task-5. The board-facing wake numbering stays in `index.yaml` as a
  cross-reference.

## Decisions locked in (the three deferred items the manager flagged)

### D1 — Enum validation location: **repository Python (no DB CHECK)**

Manager's deferred decision #1.

**Decision:** Python-layer validation only. Two lines of defense:

1. **Pydantic model** (`BehaviorFlag.flag_type: BehaviorFlagType` /
   `BehaviorFlag.confidence: Literal["high","medium","low"]` /
   `Directive.status: DirectiveStatus`) — Pydantic v2 validates on
   construction; invalid values raise `ValidationError`.
2. **Repository defensive guard** on insert: `if flag.flag_type not in
   BehaviorFlagType: raise ValueError(...)`. Cheap; catches the case
   where someone constructs a model with `model_construct()` (skips
   validation) and tries to persist it.

**Why not DB CHECK:**

- SQLite supports CHECK constraints, but they're tied to table-creation
  time. Without Alembic, evolving the enum requires deleting the DB.
- Existing convention: `events.event_type` has no CHECK and the project
  has been disciplined about Pydantic on the way in. Consistency wins.
- The "raw SQL writes bypass repository" attack surface is minimal in
  this codebase (no operator tools, no admin SQL panel; only the
  repository writes).

**Death-test requirement:** `BehaviorFlagsRepository.insert(flag)` must
reject a model constructed via `model_construct(flag_type="invalid")`
with a clear `ValueError`, never silently insert.

### D2 — One PR vs. split: **one PR with 5 internal tasks**

Manager's deferred decision #2.

**Decision:** ship as one PR. 5 internal tasks for review-gate ordering
(task-1 = scaffold/schemas, task-2 = behavior_flags, task-3 = directives,
task-4 = segmenter, task-5 = metrics). Tasks 2–5 depend only on task-1.

**Why one PR:**

- ~600–900 LoC + tests is at the upper end but within review tolerance
  (GUR-99 shipped 1266 lines of test code in one file under one PR).
- SD update lands once with the code; reviewer sees the whole contract.
- Splitting into 4 PRs creates 4 transitional states where `analysis/`
  has some pieces missing — easy for an intermediate state to ship
  accidentally because tests pass on partial deliverables.
- The 5 tasks are weakly coupled (no behavior_flags ↔ directives
  cross-dependency, segmenter doesn't know about either table); review
  burden is naturally segmentable inside one PR.

**Death-test requirement (cross-task):** `validate-and-ship` must verify
all 5 tasks done before merging. `index.yaml` `exit_criteria` enforces this.

### D3 — Same-PR SD update: **encoded as acceptance criterion + ship-manifest gate**

Manager's deferred decision #3.

**Decision:** acceptance.yaml includes a `coverage_type: verified` scenario
that asserts the PR diff includes both:

- `docs/system_design.md` §5.5.2 patched with `confidence` field in the
  output schema.
- `docs/system_design.md` §7.4 patched with `disabled_at` and
  `disabled_reason` columns.

`ship-manifest.yaml` (validate-and-ship phase) blocks merge unless
`git diff` against base shows both edits.

**Why this gate, not a softer one:**

- Memory contract `project_behaviorflag_schema_contract.md` mandates
  same-PR SD update. Soft-asking the reviewer to remember has failed
  before; a hard gate is the deterministic path.
- The SD is the canonical contract for downstream agents (Karpathy
  consumes §5.5.2 to render prompts; GUR-104 consumes §7.4 to scaffold
  the API). Drift between code and SD is invisible until someone
  notices a flag the prompt didn't predict.

**Death-test requirement:** ship-manifest verifier scripts a `git diff
docs/system_design.md` line-grep against base. Failed grep = failed
merge.

## Gaps I cannot resolve from Research

**None blocking.** Research + code-read resolved every "I cannot tell
if Research intended X or Y" question. The three deferred decisions
above were explicit planning items, not gaps.

## Minor uncertainties (documented as undocumented assumptions per skill rule)

- **U1 — `read_project_file` analysis tool surface (SD §5.4):** out of
  scope per kickoff OoS-7, but I'm assuming the segmenter's
  `SegmentData` doesn't need to carry a project-file content map.
  GUR-101 will request file content lazily at analysis time. If
  Karpathy comes back with "I need pre-loaded file content in the
  segment," that's a separate ticket.
- **U2 — Concurrent writers to `behavior_flags` / `directives`:**
  the only writer in v1 is the analyzer (single-threaded per session);
  the only reader is the dashboard (read-only). I'm not adding row
  locks or version columns. If GUR-101 introduces a parallel
  multi-session analyzer, this assumption breaks.
- **U3 — JSON-encoded TEXT for `event_ids` and `source_sessions`:**
  per SD §7.3 / §7.4, both are stored as JSON arrays in TEXT columns.
  Mirrors `events.data` convention. Repository encodes/decodes;
  callers see `list[str]`.
- **U4 — `expires_at` column logic:** SD §7.4 has `expires_at` and the
  status enum includes `expired`. I'm assuming GUR-100 only stores
  the column (nullable, accepts datetime) — checking expiry and
  transitioning `active → expired` is GUR-101's analyzer logic, not
  GUR-100's repository.

## Output state

- **Status:** `no blocking gaps — proceed to Step 2`.
- **Decisions made (D1, D2, D3):** locked-in per manager nudge mandate.
- **Assumptions documented (A1–A9, U1–U4):** carried forward to
  `2-plan.md` Tech Spec assumptions section per samsara rule
  ("Accepted gaps must be carried forward").
- **What I will do next:** write `2-plan.md`, `acceptance.yaml`,
  `overview.md`, `index.yaml`, and `tasks/task-{1..5}.md`. Bring the
  bundle back via `request_confirmation` for the planning gate.
