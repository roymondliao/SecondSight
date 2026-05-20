# SecondSight

SecondSight is a local observation and analysis stack for coding-agent sessions.
It ingests hook events, stores them durably on disk and in SQLite, exposes a
FastAPI dashboard/API, and runs an analysis pipeline that turns repeated
behavior into project directives.

Today the repository contains two main surfaces:

- `src/secondsight`: the Python package, CLI, API server, storage layer,
  adapters, and analysis pipeline.
- `frontend`: the React dashboard that sits on top of the observation,
  analysis, and directives APIs.

## What It Does

At a high level, the system looks like this:

1. Agent hook events are sent to `POST /hook/{agent}/{event_type}`.
2. SecondSight normalizes them through an adapter and binds tracker-derived
   fields such as segment index and nesting depth.
3. Events are written filesystem-first into
   `~/.secondsight/projects/<project_id>/sessions/<session_id>/events/*.json`.
4. The same events are inserted into
   `~/.secondsight/projects/<project_id>/intelligence.db`.
5. The dashboard reads project-scoped APIs for:
   - observation (`/api/sessions`, segments, event timelines)
   - analysis (`/api/analysis/*`)
   - directives (`/api/directives*`)
6. Analysis can then summarize a session, detect behavior flags, aggregate
   patterns, and persist directives.

## Current Status

The implementation is solid at the storage and read-API layers, but there are
important runtime limitations you should know before operating it:

- The server does not currently expose a working `POST /api/analyze` endpoint.
- The server does not currently wire the analysis trigger into ingestion, so
  `session_end` events do not automatically launch analysis.
- The bundled runtime registers the Claude Code adapter and the test-only
  identity adapter. Codex and OpenCode adapters exist in the codebase, but are
  not yet registered in the production server.
- The safest analysis workflow today is manual:
  `secondsight analyze --project <project_id> --session <session_id> --no-server`
- The easiest way to use the dashboard is through `secondsight serve`, which
  serves the built frontend at `/dashboard/`.

This README reflects that current behavior instead of the intended future one.

## Prerequisites

- Python `>=3.14`
- `uv >= 0.5.0`
- Node.js `>= 20.19.0` + npm (needed by Vite 7 to build the dashboard)
- Claude Code if you want to use the bundled hook installer
- LLM provider credentials if you want to run analysis

## Install (Internal Staff)

If you just want to **use** SecondSight (not develop it), the repository ships
a one-command installer that detects toolchain prerequisites, builds the
frontend, and installs the `secondsight` CLI globally via `uv tool install`.

```bash
git clone <enterprise-ghe>/SecondSight.git
cd SecondSight
./install.sh
```

What `install.sh` does — and does **not** — do:

- Verifies `uv >= 0.5.0` and `node >= 20.19.0` are available. If either is
  missing or too old, it aborts and prints install hints. It does **not**
  auto-install Node or uv; that mutation belongs to you, not the script.
- Builds `frontend/dist` via `npm ci && npm run build`.
- Installs `secondsight` as a `uv tool` (binary lands in `~/.local/bin/`).
- Does **not** touch `~/.claude/`, `~/.bashrc`, or any other dotfile. Hook
  injection and Claude settings mutation are opt-in via `secondsight init`.
- On any failure, exits non-zero without cleanup so you can see exactly which
  step broke. Re-running `./install.sh` is idempotent.

After install, opt in to Claude Code hook injection and start the daemon:

```bash
secondsight init                # writes ~/.claude/settings.json + ~/.claude/hooks/
secondsight serve --daemon
secondsight status
```

Open the dashboard at `http://127.0.0.1:8420/dashboard/`.

To remove SecondSight:

```bash
./uninstall.sh
```

The uninstaller removes the `uv tool` install but does **not** clean
`~/.claude/` or `~/.secondsight/` — those mutations belong to `secondsight
init` and the running daemon, so they must be removed with explicit user
action. The script prints manual cleanup hints.

## Developer Setup

The sections below describe the **manual / development** workflow used when
working on the codebase itself. If you only want to run SecondSight as a tool,
use the staff installer above instead.

### Backend Setup

Create the virtual environment and install Python dependencies:

```bash
uv sync
source .venv/bin/activate
```

The package exposes the `secondsight` CLI:

```bash
uv run secondsight --help
```

By default SecondSight stores data in:

```text
~/.secondsight
```

You can override that with `SECONDSIGHT_HOME` or `--home`.

### Frontend Setup

Install frontend dependencies:

```bash
cd frontend
npm install
```

Build the production dashboard bundle:

```bash
npm run build
```

The backend serves that bundle from `/dashboard/` when `frontend/dist` exists.

## Quick Start

### 1. Start the API server

Foreground:

```bash
uv run secondsight serve
```

Daemon mode:

```bash
uv run secondsight serve --daemon
uv run secondsight status
uv run secondsight serve --stop
```

Health check:

```bash
curl http://127.0.0.1:8420/health
```

### 2. Open the dashboard

With the server running and the frontend built:

```text
http://127.0.0.1:8420/dashboard/
```

The dashboard has three project-scoped views:

- Observation: sessions, segments, raw event timeline
- Analysis: session reports, trend charts, flag distributions
- Directives: directive lifecycle and effectiveness surface

### 3. Install Claude Code hooks

The repository ships hook scripts plus a `secondsight init` command that copies
them into `~/.claude/hooks/` and patches `~/.claude/settings.json`.

```bash
uv run secondsight init
```

Dry-run mode:

```bash
uv run secondsight init --dry-run
```

JSON output:

```bash
uv run secondsight init --format json
```

### 4. Ingest events

The thin ingress API expects a JSON body with transport-owned metadata plus the raw agent payload:

```json
{
  "event_id": "evt-0001",
  "timestamp": "2026-05-11T10:00:00Z",
  "sequence_number": 0,
  "payload": {
    "session_id": "session-001",
    "cwd": "/Users/example/work/example-project",
    "hook_event_name": "SessionStart"
  }
}
```

You can smoke-test ingestion manually:

```bash
curl -X POST http://127.0.0.1:8420/hook/claude_code/session_start \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "evt-0001",
    "timestamp": "2026-05-11T10:00:00Z",
    "sequence_number": 0,
    "payload": {
      "session_id": "session-001",
      "cwd": "/Users/example/work/example-project",
      "hook_event_name": "SessionStart"
    }
  }'
```

The filesystem-first write path means each event lands on disk even if the DB
insert fails. Failed DB writes are recorded in `sync.log` for later recovery.

## Daily Operator Workflow

### Observe raw sessions

Once events exist for a project:

- open `/dashboard/`
- enter the project id
- inspect sessions, segments, and event payloads in the Observation view

You can also query the observation API directly:

```bash
curl 'http://127.0.0.1:8420/api/sessions?project_id=example-project'
curl 'http://127.0.0.1:8420/api/sessions/session-001/segments?project_id=example-project'
curl 'http://127.0.0.1:8420/api/sessions/session-001/segments/0?project_id=example-project'
```

### Run analysis manually

Because server-side analysis dispatch is not wired yet, run analysis from the
CLI in-process:

```bash
uv run secondsight analyze \
  --project example-project \
  --session session-001 \
  --no-server
```

Notes:

- The current CLI analysis path uses built-in global defaults for model
  selection unless you add project-local config.
- The default primary model path is the Claude Code adapter default, so in
  practice you should expect to provide the relevant provider credentials
  before running analysis.

Retry failed runs:

```bash
uv run secondsight analyze \
  --project example-project \
  --retry-failed \
  --no-server
```

Analysis writes:

- `analysis_runs`
- `behavior_flags`
- `session_reports`
- `directives`
- `sessions/<session_id>/session_report.json`

After that, the Analysis and Directives dashboard views become useful.

### Manage directives

List active directives:

```bash
uv run secondsight directive --project example-project --active
```

Disable one:

```bash
uv run secondsight directive \
  --project example-project \
  --disable <directive_id> \
  --reason "obsolete guidance"
```

Re-enable it:

```bash
uv run secondsight directive \
  --project example-project \
  --enable <directive_id>
```

### Recover or rebuild storage

Replay `sync.log` and raw filesystem traces back into SQLite:

```bash
uv run secondsight sync
```

Limit to one project:

```bash
uv run secondsight sync --project-id example-project
```

Full DB rebuild from raw traces:

```bash
uv run secondsight sync --project-id example-project --rebuild
```

Clean up expired raw traces and analysis artifacts:

```bash
uv run secondsight cleanup --dry-run
uv run secondsight cleanup
```

## Storage Layout

Per project, SecondSight uses:

```text
~/.secondsight/
  config.toml
  fallback_events.jsonl
  logs/
  projects/
    <project_id>/
      config.toml
      intelligence.db
      sync.log
      sessions/
        <session_id>/
          events/
            *.json
          session_report.json
```

Important files:

- `intelligence.db`: canonical query store for the API and dashboard
- `sessions/*/events/*.json`: source-of-truth raw event archive
- `sync.log`: events that hit the filesystem but failed DB insertion
- `fallback_events.jsonl`: hook-side fallback spool containing raw ingress replay records when the server is down

## Configuration

Two config locations are recognized:

- global: `~/.secondsight/config.toml`
- per-project: `~/.secondsight/projects/<project_id>/config.toml`

Retention settings are currently the clearest fully-wired config surface:

```toml
[retention]
raw_traces_ttl_days = 90
analysis_ttl_days = 365
cleanup_after_analysis = false
```

Per-project analysis tool settings are also consumed:

```toml
[analysis.read_project_file]
enabled = true
size_cap_kb = 256
denylist = [".env", "secrets"]
```

The codebase also defines model-selection config types under `[analysis]`, but
the current CLI path still uses built-in global defaults for model selection.

## Frontend Development Notes

The production path is simple:

1. build `frontend/dist`
2. run `secondsight serve`
3. open `/dashboard/`

The standalone Vite dev server is less convenient right now because the
dashboard fetches `/api/*` relative to `window.location.origin`, and the Vite
config does not define an API proxy. In practice that means:

- `npm run dev` is fine for UI-only work
- for end-to-end dashboard testing, serve the built assets through the Python
  backend, or add your own local proxy

## API Surface

Main endpoints:

- `POST /hook/{agent}/{event_type}`
- `POST /hook/session-start`
- `GET /health`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}`
- `GET /api/sessions/{session_id}/segments`
- `GET /api/sessions/{session_id}/segments/{segment_index}`
- `GET /api/analysis/summary`
- `GET /api/analysis/sessions`
- `GET /api/analysis/sessions/{session_id}`
- `GET /api/analysis/sessions/{session_id}/flags`
- `GET /api/analysis/trends`
- `GET /api/analysis/aggregation`
- `GET /api/directives`
- `PATCH /api/directives/{directive_id}`

All dashboard read APIs are project-scoped and require `project_id`.

## Development Commands

Backend tests:

```bash
source .venv/bin/activate
pytest
```

Formatting and linting:

```bash
source .venv/bin/activate
uv run pre-commit run --all-files
```

Frontend build:

```bash
cd frontend
npm run build
```

## Known Gaps

- No working `POST /api/analyze` route yet
- No automatic analysis dispatch on `session_end`
- Server runtime currently registers Claude Code only, not Codex/OpenCode
- Global model-selection config is defined in code but not fully loaded by the
  CLI analysis path
- Vite dev mode does not proxy backend API requests
