# SecondSight Architecture

This document is a short map of the current system. The README stays focused on
user setup and tutorial flow.

## Runtime Shape

SecondSight is a local single-user service:

1. Agent hooks call the local HTTP server.
2. The server selects an adapter for the agent and normalizes the payload.
3. A session tracker adds segment and nesting metadata.
4. The observation pipeline writes raw traces first, then inserts into SQLite.
5. The dashboard and APIs read from the per-project SQLite database.
6. Analysis converts sessions into reports, behavior flags, and directives.
7. Directive injection can feed active project guidance back into future agent
   sessions.

The server binds to `127.0.0.1` by default and serves the dashboard from
`/dashboard/` when the bundled or locally built frontend assets are available.

## Main Components

- `src/secondsight/cli`: Typer CLI commands such as `init`, `serve`, `status`,
  `analyze`, `directive`, `sync`, `cleanup`, and `config`.
- `src/secondsight/api`: FastAPI server, hook ingestion, observation APIs,
  analysis read APIs, directive APIs, and hook injection endpoints.
- `src/secondsight/adapters`: Agent-specific normalization for Claude Code,
  Codex, OpenCode, and test identity payloads.
- `src/secondsight/observation`: Session tracking and filesystem-first ingest.
- `src/secondsight/storage`: SQLite tables, repositories, raw trace stores, and
  backfill support.
- `src/secondsight/analysis`: Analysis orchestration, dispatch, output recovery,
  aggregation, and runtime construction.
- `src/secondsight/feedback`: Directive lifecycle, convention selection, and
  prompt/hook injection support.
- `frontend`: React dashboard.
- `scripts/hooks`: Bundled hook scripts copied by `secondsight init`.

## Storage Layout

Default home:

```text
~/.secondsight/
  config.toml
  state.json
  fallback_events.jsonl
  logs/
  projects/
    <project_id>/
      intelligence.db
      sync.log
      sessions/
        <session_id>/
          events/
            *.json
          session_report.json
```

Raw traces are the durable source of truth. If a DB insert fails after the raw
write succeeds, the failure is recorded in `sync.log` and can be replayed with
`secondsight sync`.

## Analysis Dispatch

Analysis can start from three paths:

- event-driven: a `session_end` event triggers analysis after ingest
- timeout-driven: the server sweeper finds stale sessions
- manual: `secondsight analyze --project <project_id> --session <session_id> --no-server`

The default analysis mode is `cli`, which spawns the selected coding-agent CLI
and uses that tool's own auth. `sdk` mode uses provider API keys configured in
`~/.secondsight/config.toml`.

There is no public `POST /api/analyze` route yet. Use `--no-server` for manual
analysis until that API exists.

## Configuration

`secondsight init` generates:

```text
~/.secondsight/config.toml
```

Important sections:

- `[general]`: mode and log level
- `[analysis.cli]`: default coding agent and per-agent model override
- `[analysis.sdk]`: direct provider model settings
- `[providers.*]`: SDK provider credentials
- `[feedback]`: convention and injection budgets
- `[directive_lifecycle]`: directive lifecycle policy
- `[retention]`: raw trace and analysis TTL
- `[server]`: host, port, and daemon startup setting

Validate config with:

```bash
secondsight config validate
```

Show the effective config:

```bash
secondsight config show
```
