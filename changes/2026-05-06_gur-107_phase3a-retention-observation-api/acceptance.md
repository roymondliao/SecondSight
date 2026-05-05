# Acceptance Criteria: GUR-107a — Observation API + raw_traces Retention

> Conditional on board confirmation `a0a92005` resolving to **(A) Split**.

## Functional

1. **A-F1** `GET /api/sessions?project_id={pid}` returns a JSON
   `{sessions: [...], next_cursor: ...}` shape; rejects requests
   without `project_id` with 422 (DC-4).
2. **A-F2** `GET /api/sessions/{sid}?project_id={pid}` returns
   `{session_id, project_id, first_event_at, last_event_at,
   event_count, segment_count}`; 404 on unknown `(sid, pid)` pair.
3. **A-F3** `GET /api/sessions/{sid}/segments?project_id={pid}`
   returns `{segments: [{segment_index, event_count,
   first_event_at, last_event_at}]}` ordered by `segment_index`.
4. **A-F4** `GET /api/sessions/{sid}/segments/{idx}?project_id={pid}`
   returns `{events: [Event]}` with full payload from `data` JSON
   column; 404 on unknown segment.
5. **A-F5** `secondsight cleanup --dry-run` walks every project
   under `~/.secondsight/projects/`, prints one line per
   to-be-deleted session, and exits 0 with no FS/DB writes.
6. **A-F6** `secondsight cleanup` performs the deletions reported
   by `--dry-run`. A second invocation immediately after reports
   nothing.
7. **A-F7** `secondsight cleanup --project-id PID` scopes to one
   project (with or without `--dry-run`).

## TTL Resolution

8. **A-T1** Per-project `config.toml` `[retention] raw_traces_ttl_days = N`
   overrides the global default; cleanup logs
   `source=per_project_config` for that project.
9. **A-T2** Global `~/.secondsight/config.toml`
   `[retention] raw_traces_ttl_days = N` is used when per-project
   omits the key; logs `source=global_config`.
10. **A-T3** Built-in default of 90 days applies when neither file
    has the key; logs `source=builtin_default`.
11. **A-T4** Malformed per-project TOML raises
    `RetentionConfigError`; cleanup exits non-zero and reports the
    project_id (DC-6). Cleanup of OTHER projects is unaffected
    (per-project errors do not abort the whole run).

## Boundary correctness

12. **A-B1** A session whose newest event is younger than TTL is
    NEVER reaped, even if its first event is older than TTL (DC-2).
13. **A-B2** `--dry-run` and real run enumerate identical session
    sets given identical inputs (DC-3).
14. **A-B3** Partial-failure cleanup (FS removed, DB delete throws)
    logs ERROR with session_id and exits non-zero (DC-5). The
    process does not silently continue.

## ETag / pagination

15. **A-E1** `GET /api/sessions` returns an ETag header. Repeating
    the call with `If-None-Match: <etag>` yields 304 with no body
    while underlying state is unchanged.
16. **A-E2** A new event appended to the project changes the
    project-level ETag.
17. **A-E3** `?limit=N&offset=M` on listing endpoints returns
    bounded result sets; default 100, max 500.

## Test coverage gate

18. **A-X1** New tests live under
    `tests/storage/test_retention.py`,
    `tests/api/test_observation.py`,
    `tests/cli/test_cleanup.py`.
19. **A-X2** Each task ships death tests first (samsara
    discipline). DC-1 through DC-7 in `2-plan.md` §5 are all
    represented as named tests.
20. **A-X3** Full suite stays at or above the prior commit's pass
    count; no Phase 1 regressions.

## Documentation

21. **A-D1** SD §3.10 reference confirmed; if implementation
    deviates, `2-plan.md` decisions are updated and the SD
    reference is annotated.
22. **A-D2** `secondsight cleanup --help` lists `--dry-run`,
    `--project-id`, and the exit code semantics.

## Out-of-scope assertion (explicit non-goals)

These are NOT acceptance criteria for this scope:

- ❌ `analysis_ttl_days` enforcement — GUR-107b
- ❌ Post-analysis cleanup trigger — GUR-107b
- ❌ Behavior flags / directives endpoints — GUR-104
- ❌ Cross-project listing endpoint — out by single-project MVP
- ❌ Streaming endpoints — D6
