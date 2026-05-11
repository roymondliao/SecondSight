"""End-to-end integration test for ClaudeCodeAdapter (task-5, P1-9-int).

This test is the integration **death case** for phase1-adapters: every prior
task passes in isolation (unit + fixture round-trip), but only this test
proves the full pipeline is wired:

    POST /hook/{event_type}
        → FastAPI route handler (api/hooks.py)
        → AdapterRegistry.for_("claude_code", event_type)  (registered in lifespan)
        → ClaudeCodeAdapter.normalize(envelope, event_type)  → PartialEvent
        → SessionTracker.bind(partial)  → Event
        → ObservationPipeline.ingest(event)  → SQLite + RawTraceStore

Failure modes this test catches that no prior task can:

    1. Adapter declared but not registered in `lifespan` (server.py).
    2. Drop_list correct in adapter unit test but tracker.bind() or
       pipeline.ingest() copies dropped fields back into Event.data.
    3. Fixture round-trip green but route validation (path-safety,
       enum closure) silently strips required fields.
    4. Lifespan startup order regression: ClaudeCodeAdapter registered AFTER
       IdentityAdapter so IdentityAdapter wins dispatch on overlap.

Why this is its own death test
------------------------------
Task-5 has no separate unit/death-test split because the test IS the death
test: north-star fidelity = 1.0 across every P1-floor fixture, with the
privacy canary absent from every stored Event.data. If the pipeline ever
loses a field or leaks the canary, this test fails red.

The non-obvious silent failure this test guards
-----------------------------------------------
A test that loops zero fixtures still PASSES. That would silently mask the
fidelity contract. The `test_p1_floor_coverage` assertion below makes the
fixture count a load-bearing precondition: if a fixture is deleted without
updating the integration test, the test fails loudly rather than silently
shrinking.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from secondsight.api.server import create_app

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code"

# P1 floor — must match plan §7 G1 / tests/adapters/test_fixtures.py.
# Duplicated here intentionally: this set is the integration test's
# wiring contract, independent of fixture-discovery code. If the floor
# shifts, BOTH this set and test_fixtures.P1_HOOK_EVENT_NAMES must be
# updated — surfacing the drift loudly via test_p1_floor_coverage.
#
# Naming convention: filenames are snake_case of the Claude Code hook
# event name, optionally suffixed with the tool variant. The mapping is
# NOT one-to-one with test_fixtures.P1_HOOK_EVENT_NAMES (which holds the
# 5 hook event names): "PreToolUse" maps to "pre_tool_use_bash.json"
# today because Bash is the only verified-source tool variant. A future
# Phase-2 fixture refresh adding e.g. "pre_tool_use_read.json" would
# expand THIS set without changing P1_HOOK_EVENT_NAMES — the set
# equality check below would block such a change until this constant
# (and the discovery contract) is intentionally extended. That is the
# desired loud-failure path, not an accidental one.
EXPECTED_FIXTURE_FILENAMES: frozenset[str] = frozenset(
    {
        "pre_tool_use_bash.json",
        "post_tool_use.json",
        "user_prompt_submit.json",
        "session_start.json",
        "session_end.json",
    }
)

PRIVACY_CANARY = "PRIVACY_CANARY_DO_NOT_STORE"

# Bounded poll for fire-and-forget ingest to commit to SQLite. The handler
# returns 200 before pipeline.ingest writes the row; we poll instead of a
# bare `time.sleep` so the test stays fast on the happy path and gives a
# real failure mode (timeout, not flake) on regression.
_INGEST_POLL_TIMEOUT_S = 2.0
_INGEST_POLL_INTERVAL_S = 0.02


def _load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_event_row(db_path: Path, event_id: str) -> dict[str, Any]:
    """Poll the per-project SQLite until the event row appears.

    Raises a clear AssertionError on timeout — that signals a real wiring
    failure (ingest task never wrote the row), not a flaky test. We open
    a fresh sqlite3 connection per poll so we always see committed state
    from the pipeline's writer connection.
    """
    deadline = time.monotonic() + _INGEST_POLL_TIMEOUT_S
    last_count: int | None = None  # None = DB file never appeared
    while time.monotonic() < deadline:
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, session_id, event_type, data FROM events WHERE id = ?",
                    (event_id,),
                ).fetchone()
                if row is not None:
                    return {k: row[k] for k in row.keys()}
                last_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        time.sleep(_INGEST_POLL_INTERVAL_S)
    state = (
        "DB file never appeared during poll window"
        if last_count is None
        else f"events table count at last poll: {last_count}"
    )
    raise AssertionError(
        f"Event row id={event_id!r} did not appear in {db_path} within "
        f"{_INGEST_POLL_TIMEOUT_S}s ({state}). Likely cause: pipeline.ingest "
        f"task crashed silently, or adapter not registered in lifespan, or "
        f"per-project resources never materialised."
    )


def _check_partial_match(actual: Any, expected: Any, path: str) -> list[str]:
    """Return list of mismatch descriptions; empty list on full match.

    "Partial" semantics: every key listed in `expected` must be present in
    `actual` with an equal value. Extra keys in `actual` are allowed (the
    adapter is free to add metadata; we only assert on the contracted shape).
    """
    mismatches: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected dict, got {type(actual).__name__}"]
        for k, v in expected.items():
            sub_path = f"{path}.{k}" if path else k
            if k not in actual:
                mismatches.append(f"{sub_path}: missing (expected {v!r})")
                continue
            mismatches.extend(_check_partial_match(actual[k], v, sub_path))
    else:
        if actual != expected:
            mismatches.append(f"{path}: expected {expected!r}, got {actual!r}")
    return mismatches


# ---------------------------------------------------------------------------
# Coverage guard — loop-ate-zero-fixtures protection
# ---------------------------------------------------------------------------


def test_p1_floor_coverage() -> None:
    """The fixture set discovered on disk must equal the P1 floor exactly.

    Without this guard, deleting a fixture would silently shrink the
    integration test's contract surface — the fidelity assertion would
    still report 1.0 over the smaller set. Coupling the discovered set to
    the declared floor is the only way to make missing-fixture regressions
    fail loudly.
    """
    discovered = {p.name for p in FIXTURE_DIR.glob("*.json")}
    missing = EXPECTED_FIXTURE_FILENAMES - discovered
    extra = discovered - EXPECTED_FIXTURE_FILENAMES
    assert not missing and not extra, (
        f"Fixture set drift — expected exactly {sorted(EXPECTED_FIXTURE_FILENAMES)}, "
        f"missing: {sorted(missing)}, extra: {sorted(extra)}. "
        f"Update both this set and tests/adapters/test_fixtures.P1_HOOK_EVENT_NAMES "
        f"in the same commit."
    )


# ---------------------------------------------------------------------------
# North-star metric: claude_code_event_normalization_fidelity == 1.0
# ---------------------------------------------------------------------------


def test_fidelity_against_fixtures(tmp_path: Path) -> None:
    """For every P1-floor fixture: hook → adapter → tracker → pipeline → DB.

    Asserts:
      * 200 OK from POST /hook/{event_type}.
      * Row appears in per-project SQLite within bounded poll window.
      * fidelity ratio = matched_fields / expected_fields == 1.0.
      * privacy canary string never appears in Event.data JSON serialisation.

    Each fixture runs in its own session (sequence_number=0) to keep the
    test independent of session-tracker ordering invariants — those are
    covered by tracker unit tests, not this integration assertion.
    """
    home = tmp_path / ".secondsight"
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(exist_ok=True)

    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixtures, f"No fixtures discovered under {FIXTURE_DIR}. Re-check task-2 deliverable."

    project_id = "proj-int-claude-code"
    canary_violations: list[str] = []
    fixtures_exercised: set[str] = set()

    app = create_app(secondsight_home=home)
    # `raise_server_exceptions=False` is the project convention for hook
    # tests: it lets the route's HTTPException → status_code path be the
    # signal we assert on, instead of bubbling Python exceptions through
    # the TestClient boundary and short-circuiting the loop. A regression
    # that returns 500 still fails the `status_code == 200` assertion below.
    with TestClient(app, raise_server_exceptions=False) as client:
        for fixture_path in fixtures:
            fx = _load_fixture(fixture_path)
            payload = fx["payload"]
            event_type = fx["_meta"]["_secondsight_event_type"]
            expected = fx["expected_partial_event_data"]

            # Synthesize envelope-level fields (per task-5 spec). Use a fresh
            # session per fixture so each can use sequence_number=0 without
            # crossing the UNIQUE(session_id, sequence_number) constraint.
            stem = fixture_path.stem
            session_id = f"sess-int-{stem}"
            event_id = f"evt-int-{stem}"
            envelope = {
                "project_id": project_id,
                "session_id": session_id,
                "agent": "claude_code",
                "event_id": event_id,
                "timestamp": datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "sequence_number": 0,
                "payload": payload,
            }

            response = client.post(f"/hook/{event_type}", json=envelope)
            assert response.status_code == 200, (
                f"{fixture_path.name}: POST /hook/{event_type} returned "
                f"{response.status_code}. Body: {response.text}"
            )
            assert response.json() == {"status": "ok"}

            db_path = home / "projects" / project_id / "intelligence.db"
            row = _wait_for_event_row(db_path, event_id)

            # Sanity: stored event_type must match the route param —
            # silent route/payload mismatch would corrupt analytics queries.
            assert row["event_type"] == event_type, (
                f"{fixture_path.name}: stored event_type={row['event_type']!r} "
                f"!= POSTed {event_type!r}"
            )

            stored_data: dict[str, Any] = json.loads(row["data"])

            # Privacy assertion: canary never reaches Event.data, regardless
            # of how it was placed in the payload (Bash command, prompt,
            # tool_response.output, or session_id field — fixtures vary).
            # Accumulated rather than asserted in-loop so a fidelity failure
            # in fixture N does not mask a canary leak in fixture M > N;
            # both classes of regression report at end-of-run.
            stored_data_json = json.dumps(stored_data, ensure_ascii=False)
            if PRIVACY_CANARY in stored_data_json:
                canary_violations.append(
                    f"{fixture_path.name}: canary leaked into Event.data — "
                    f"data JSON: {stored_data_json}"
                )

            # Vacuous-pass guard: an empty `expected_partial_event_data`
            # would produce zero mismatches and silently make this fixture
            # contribute nothing to the fidelity contract. Reject at the
            # fixture level rather than letting the loop stay green.
            assert _count_leaf_fields(expected) > 0, (
                f"{fixture_path.name}: expected_partial_event_data has zero "
                f"leaf fields — fixture would pass vacuously without asserting "
                f"anything. Re-check fixture authorship."
            )

            # Fidelity = 1.0 is, by spec (task-5.md step 7), the absence of
            # any expected leaf that is missing or unequal in stored_data.
            # `_check_partial_match` returns named-path mismatches; an empty
            # list IS the fidelity == 1.0 witness. We do not maintain a
            # separate ratio scalar — its arithmetic only stays honest on
            # the all-green path, and a redundant bundle-level
            # `all(ratio == 1.0)` cannot fail any case the per-fixture
            # `assert not mismatches` does not catch first.
            mismatches = _check_partial_match(stored_data, expected, "")
            assert not mismatches, (
                f"{fixture_path.name}: fidelity != 1.0 "
                f"({len(mismatches)} mismatch(es)):\n  - "
                + "\n  - ".join(mismatches)
                + f"\n\nstored Event.data: {stored_data!r}\n"
                + f"expected: {expected!r}"
            )

            fixtures_exercised.add(fixture_path.name)

    # Bundle-level invariants: every fixture actually ran; no canary leaks
    # anywhere. The exercised-set guard catches a regression where a future
    # `continue` or early-break inside the loop silently skips fixtures.
    assert fixtures_exercised == {p.name for p in fixtures}, (
        f"Not every fixture was exercised — loop short-circuited. "
        f"Exercised: {sorted(fixtures_exercised)}, "
        f"discovered: {sorted(p.name for p in fixtures)}"
    )
    assert not canary_violations, (
        "Privacy canary leaked into Event.data for one or more fixtures:\n  - "
        + "\n  - ".join(canary_violations)
    )


def _count_leaf_fields(expected: Any) -> int:
    """Count terminal (non-dict) fields in `expected`.

    Used as the vacuous-pass guard: a fixture whose
    `expected_partial_event_data` is `{}` (or a nested-only structure with
    no leaves) would produce zero mismatches and contribute nothing to the
    fidelity contract. Counting leaves rejects that shape at the per-fixture
    assertion. NOT used as a fidelity denominator: the spec's
    "matched/expected == 1.0" reduces to "no mismatches" given a non-empty
    leaf set, and we assert that directly.
    """
    if isinstance(expected, dict):
        return sum(_count_leaf_fields(v) for v in expected.values())
    return 1
