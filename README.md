# SecondSight

SecondSight is a local observation and analysis tool for coding-agent sessions.
It records agent hook events, stores them under `~/.secondsight`, serves a local
dashboard, and turns repeated behavior into project directives that can be
injected back into future agent sessions.

The current workflow is intentionally local-first:

- capture Claude Code or Codex hook events
- browse sessions, segments, and timelines in the dashboard
- run or trigger session analysis
- review and manage generated directives
- keep raw traces on disk so SQLite can be rebuilt if needed

For implementation details, see [docs/architecture.md](docs/architecture.md).
For HTTP endpoints, see [docs/api.md](docs/api.md).

## Prerequisites

- Python 3.14
- `uv >= 0.5.0`
- Node.js `>= 20.19.0` and npm, for building the dashboard
- Claude Code or Codex, if you want automatic hook capture

Analysis uses either your coding-agent CLI auth (`mode = "cli"`, default) or
direct provider API keys (`mode = "sdk"`). Configuration is generated at
`~/.secondsight/config.toml`.

## Install

For normal use, run the installer from the repository root:

```bash
git clone <repo-url>
cd SecondSight
./install.sh
```

The installer checks `uv`, Node, and npm; builds the dashboard; and installs the
`secondsight` CLI with `uv tool install`. It does not modify your agent config.

Initialize hooks explicitly:

```bash
secondsight init --agent claude_code
```

or:

```bash
secondsight init --agent codex
```

Then start the local server:

```bash
secondsight serve --daemon
secondsight status
```

Open:

```text
http://127.0.0.1:8420/dashboard/
```

## Tutorial

### 1. Initialize SecondSight

`secondsight init` copies bundled hook scripts into the selected agent home,
updates the agent hook registration, writes `~/.secondsight/state.json`, and
generates `~/.secondsight/config.toml`.

Preview changes without writing:

```bash
secondsight init --agent claude_code --dry-run
```

If your config already exists and is missing newer keys:

```bash
secondsight init --merge-config
secondsight config validate
```

### 2. Start The Server

Run in the foreground while developing:

```bash
secondsight serve
```

Run as a background daemon:

```bash
secondsight serve --daemon
secondsight status
```

Stop it:

```bash
secondsight serve --stop
```

### 3. Capture A Session

Start a normal Claude Code or Codex session in a project after hooks are
installed. SecondSight derives the `project_id` from the working directory name
and writes data under:

```text
~/.secondsight/projects/<project_id>/
```

Useful files:

- `intelligence.db`: SQLite query store for the dashboard and APIs
- `sessions/<session_id>/events/*.json`: raw event archive
- `sync.log`: events that reached disk but need DB backfill
- `~/.secondsight/fallback_events.jsonl`: hook fallback spool when the server is down

### 4. Use The Dashboard

Open the dashboard and enter the project id:

```text
http://127.0.0.1:8420/dashboard/
```

The dashboard is organized around:

- Observation: sessions, segments, and event timelines
- Analysis: session reports, behavior flags, and trends
- Directives: active guidance generated from repeated behavior

### 5. Run Analysis

When the server observes a clean `session_end`, SecondSight can dispatch
analysis automatically. A background sweeper also handles stale sessions whose
agent process did not exit cleanly.

For an explicit manual run, use the in-process path:

```bash
secondsight analyze \
  --project <project_id> \
  --session <session_id> \
  --no-server
```

Retry failed analysis runs:

```bash
secondsight analyze \
  --project <project_id> \
  --retry-failed \
  --no-server
```

### 6. Manage Directives

List active directives:

```bash
secondsight directive --project <project_id> --active
```

Disable one:

```bash
secondsight directive \
  --project <project_id> \
  --disable <directive_id> \
  --reason "obsolete guidance"
```

Re-enable one:

```bash
secondsight directive \
  --project <project_id> \
  --enable <directive_id>
```

### 7. Repair Or Clean Up Data

Replay filesystem traces and fallback records into SQLite:

```bash
secondsight sync
```

Scope to one project:

```bash
secondsight sync --project-id <project_id>
```

Clean up expired traces and analysis artifacts:

```bash
secondsight cleanup --dry-run
secondsight cleanup
```

## Development

Install backend dependencies:

```bash
uv sync
source .venv/bin/activate
```

Build the dashboard:

```bash
cd frontend
npm ci
npm run build
```

Run the CLI from the checkout:

```bash
uv run secondsight --help
uv run secondsight serve
```

Run tests:

```bash
pytest
```

Run formatting and checks before committing:

```bash
uv run pre-commit run --all-files
```

## Uninstall

Remove the `uv tool` install:

```bash
./uninstall.sh
```

The uninstaller does not remove agent hook settings or `~/.secondsight` data.
Inspect those files and remove them manually only when you are sure you no
longer need the captured session history.
