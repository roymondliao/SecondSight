# Task 2: MH-1 + MH-2 — Single event + multi-event session sequence

## Context

Read: `overview.md` for full architecture and decisions.

This task adds two test classes to `tests/integration/test_phase1_e2e.py`:
- `TestMH1SingleEvent` — single hook fires, observable in DB and raw trace
- `TestMH2MultiEvent` — realistic sequence exercising `segment_index` increment AND sub-agent nesting, plus a stack-mismatch death case

The fixtures and helpers used (already exist in `tests/scripts/conftest.py`):
- `real_secondsight_server` — uvicorn server bound to a tmp `SECONDSIGHT_HOME`, registers a `_ClaudeCodeAdapterStub`. Yields `{port, home, project_id, session_id}`.
- `hook_script(name) -> Path` — absolute path to `scripts/hooks/<name>.sh`
- `run_hook(script, payload, *, env, timeout) -> CompletedProcess` — subprocess wrapper
- `build_env(*, port, home, agent='test-agent', extra=None)` — env dict for hook subprocess

Critical facts from code reading (do NOT contradict):
- `segment_index` increments by exactly 1 ONLY on `EventType.USER_PROMPT` (`src/secondsight/observation/tracker.py:193-195`). Pre/post-tool-use events do NOT increment.
- Sub-agent nesting uses `EventType.SUB_AGENT_START`/`SUB_AGENT_END` events with `data["sub_agent_id"]` (NOT a `parent_agent_id` field on tool-use events).
- `SubAgentStackMismatch` is raised by tracker when `sub_agent_end` arrives on empty stack or with mismatched id. The hook router translates this into HTTP 4xx (verify exact code by reading `src/secondsight/api/hooks.py` before assertion).
- DB path: `<home>/projects/proj-test/intelligence.db`. Raw traces: `<home>/projects/proj-test/sessions/<session_id>/events/*.json`.
- Hook scripts available: `pre-tool-use.sh`, `post-tool-use.sh`, `session-start.sh`, `session-end.sh`, `user-prompt.sh`, plus sub-agent variants if present (verify in `scripts/hooks/`).
- After each hook fire, server runs ingest as fire-and-forget; tests must `time.sleep(0.3)` (or poll) before asserting DB state.

## Files

- Modify: `tests/integration/test_phase1_e2e.py` — add `TestMH1SingleEvent` and `TestMH2MultiEvent` classes
- Test: same file (this IS the test file)

## Death Test Requirements

- **DT-2.1 (MH-1)** — After a successful hook fire against a live server, `fallback_events.jsonl` must NOT exist OR must have zero non-empty lines. Failure message must say "hook posted to a wrong URL — fell back instead of hitting server".
- **DT-2.2 (MH-2 segment_index frozen)** — In the 8-event session sequence, if all DB rows share `segment_index=0`, fail with message naming `tracker.bind() did not increment segment_index on USER_PROMPT`.
- **DT-2.3 (MH-2 stack mismatch)** — Firing `sub_agent_end` with a never-pushed `sub_agent_id` against a fresh session must produce HTTP 4xx AND zero DB rows for that session_id.

## Implementation Steps

- [ ] Step 1: Read `src/secondsight/api/hooks.py` to confirm exact HTTP code for `SubAgentStackMismatch` (likely 422 or 400). Record finding in test docstring.
- [ ] Step 2: Read `scripts/hooks/` to confirm which event-type scripts exist (especially `sub-agent-start.sh`, `sub-agent-end.sh`).
- [ ] Step 3: Write death tests DT-2.1, DT-2.2, DT-2.3 first. Run — verify red (test classes don't exist yet).
- [ ] Step 4: Write happy-path tests for MH-1 (1 test) and MH-2 (2 tests: session sequence + sub-agent nesting). Run — verify red.
- [ ] Step 5: Implement nothing in production — these tests must pass against existing code, OR reveal a bug, OR reveal that my plan misread the code. If they fail, do NOT fix the test until you understand WHY.
- [ ] Step 6: Run all 6 tests — confirm green.
- [ ] Step 7: Stress test: run the new tests 10 times in a loop. Note any flakes. Tighten fire-and-forget waits if needed.
- [ ] Step 8: Write scar report. Commit.

## MH-1 specifics

```python
class TestMH1SingleEvent:
    """MH-1: Single hook event traverses pipeline with verifiable evidence."""

    def test_mh1_single_event_lands_in_db(
        self, real_secondsight_server: dict[str, Any]
    ) -> None:
        # Use unique event_id 'evt-mh1-live' so assertion is unambiguous.
        # Build payload as claude-code envelope (see test_hook_fallback.py
        # for shape: project_id/session_id/agent/event_id/timestamp/sequence_number/payload).
        # Fire pre-tool-use.sh via run_hook; assert exit 0; sleep 0.3;
        # assert DB has 1 row with segment_index=0, sub_agent_id IS NULL, depth=0;
        # assert raw trace JSON exists.
        ...

    def test_mh1_no_fallback_when_server_accepts(
        self, real_secondsight_server: dict[str, Any]
    ) -> None:
        # DT-2.1: after the same fire, fallback_events.jsonl is empty/absent.
        # If present with content, the failure message must include
        # "hook posted to a wrong URL — fell back instead of hitting server".
        ...
```

## MH-2 specifics

```python
class TestMH2MultiEvent:
    """MH-2: Session sequence exercises segment_index + sub-agent nesting."""

    def test_mh2_segment_index_transitions_on_user_prompt(
        self, real_secondsight_server: dict[str, Any]
    ) -> None:
        # 8-event sequence, single session_id, sequence_number 1..8:
        #   1 session-start
        #   2 user-prompt
        #   3 pre-tool-use
        #   4 post-tool-use
        #   5 user-prompt
        #   6 pre-tool-use
        #   7 post-tool-use
        #   8 session-end
        # Expected segment_index per row (after the corresponding USER_PROMPT
        # increment is applied): 0, 1, 1, 1, 2, 2, 2, 2.
        # Assert 8 rows exist; assert segment_index transitions match.
        # If all rows share segment_index=0: fail with DT-2.2 message.
        ...

    def test_mh2_sub_agent_nesting_depth_toggles(
        self, real_secondsight_server: dict[str, Any]
    ) -> None:
        # 4-event sequence:
        #   1 user-prompt
        #   2 sub_agent_start with data.sub_agent_id='child-1'
        #   3 pre-tool-use
        #   4 sub_agent_end with data.sub_agent_id='child-1'
        # Assert depth: 0, 1, 1, 0; sub_agent_id: null, 'child-1', 'child-1', null.
        ...

    def test_mh2_sub_agent_end_on_empty_stack_rejected(
        self, real_secondsight_server: dict[str, Any]
    ) -> None:
        # DT-2.3: fire sub_agent_end with data.sub_agent_id='ghost' against
        # a fresh session_id, no prior sub_agent_start.
        # If sub-agent-end.sh does not exist as a hook script, use a direct
        # POST via curl through the existing pre-tool-use.sh harness — but
        # ONLY if necessary, because that bypasses the bash seam.
        # Preferred: confirm the hook script exists and use it.
        # Assert: hook exits 0 (envelope was valid), but server returned non-2xx.
        # Assert: DB has zero events for this session_id.
        # Assert: subsequent regular hooks for the same session work normally.
        ...
```

## Expected Scar Report Items

- Potential shortcut: using `client.post()` against `TestClient(app)` to avoid subprocess complexity — REJECTED, kickoff Step 0 commitment 1.
- Potential shortcut: relaxing fire-and-forget wait to 0.05s to "speed up tests" — REJECTED unless poll loop is added; the existing `test_hook_fallback.py::UT-1` uses 0.3s for a reason.
- Assumption to verify: hook scripts for `session-start`, `session-end`, `user-prompt`, `sub_agent_start`, `sub_agent_end` exist in `scripts/hooks/`. If sub_agent scripts are absent, surface as a gap rather than silently route through pre-tool-use.sh.
- Assumption to verify: HTTP code for `SubAgentStackMismatch` — confirm by reading `api/hooks.py`, document in test docstring.
- Assumption to verify: DB query path. Confirm by reading `tests/scripts/test_hook_fallback.py::UT-1` (already established pattern).

## Acceptance Criteria

- Covers: "Success - single event traverses full pipeline with verifiable evidence" (MH-1)
- Covers: "Silent failure - hook URL drift causes events to fall back without signal" (DT-2.1)
- Covers: "Silent failure - segment_index frozen at 0 for entire session" (DT-2.2)
- Covers: "Silent failure - sub_agent_end on empty stack silently advances depth" (DT-2.3)
- Covers: "Success - sub-agent nesting depth toggles correctly"
