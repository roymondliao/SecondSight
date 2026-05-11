# Task 4: cli/directive.py — schema-as-contract Typer subcommand

## Context

Read: `overview.md`. This task adds the agent-self-query and operator
control surface for directives. The CLI imports `DirectiveOut` and
`DirectivePatchRequest` from `api/directives.py` (task-2) so its
`--format json` output is structurally identical to the API — DC-4
defense. Server-mode dispatch follows the precedent at
`cli/analyze.py:135-165` exactly: ConnectError → silent in-process
fallback (with INFO log + stderr warning), HTTPStatusError → loud
exit (server is up, endpoint is broken; do NOT silent-fall-back).

Existing surface to study:
- `src/secondsight/cli/analyze.py:73-192` — Typer callback structure
  including `--no-server`, `--server-url`, `--home`, `--project`.
- `src/secondsight/cli/analyze.py:135-165` — fallback discipline
  (the exact pattern to replicate).
- `src/secondsight/cli/analyze.py:225-252` — `_resolve_project_id`
  pattern.
- `src/secondsight/cli/_home.py` — `secondsight_home` resolver.
- `src/secondsight/storage/directives_repository.py` — `get_active_conventions`,
  `update_status`, `get_by_id`.
- `src/secondsight/api/directives.py` (task-2 output) — import
  `DirectiveOut` and `DirectivePatchRequest` directly.

## Files

- Create: `src/secondsight/cli/directive.py`
- Test: `tests/cli/test_directive.py`

## Death Test Requirements

Write these tests **before** the implementation:

- **DT-4.1** (DC-4 byte-identical JSON): Set up an isolated
  `secondsight_home` with one directive. Run two test invocations:
  (a) start a real `create_app()` server on a random port, run the
  CLI in server-mode pointing at it; (b) run the CLI with
  `--no-server`. Capture stdout from both. Assert
  `sha256(a) == sha256(b)`.
- **DT-4.2** (ConnectError → fallback): Pass a `--server-url` that
  points to a closed port. Assert: exit 0, stderr contains
  "not reachable", stdout has the expected JSON (in-process result).
- **DT-4.3** (HTTPStatusError → loud exit): Mock `httpx.post` /
  `httpx.get` to return a 500. Assert: exit 1, stderr contains
  the HTTP status code, NO in-process fallback was attempted.
- **DT-4.4** (`--disable` requires `--reason`): Run
  `secondsight directive --disable ID` (no --reason). Assert exit 2
  (typer.BadParameter convention) with message naming the missing
  flag.
- **DT-4.5** (`--enable` no-op): Insert a directive with
  `status=active`. Run `secondsight directive --enable ID`. Assert
  exit 0 with message containing "no change" or "already active".
- **HP-4.1**: Insert 2 active directives. Run
  `secondsight directive --active --format json`. Assert stdout is
  valid JSON, parses to `list[dict]` of length 2, each item has the
  full `DirectiveOut` shape.
- **HP-4.2**: Same fixture with default format (`--active` only).
  Assert stdout contains a Rich table with columns id / type /
  summary / frequency / created_at.

## Implementation Steps

- [ ] Step 1: Write the 7 tests; commit failing.
- [ ] Step 2: Run tests — verify failures (module doesn't exist).
- [ ] Step 3: Build the Typer `app` with the subcommand callback:
  ```python
  @app.callback(invoke_without_command=True)
  def directive(
      active: bool = typer.Option(False, "--active"),
      disable: Optional[str] = typer.Option(None, "--disable"),
      enable: Optional[str] = typer.Option(None, "--enable"),
      reason: Optional[str] = typer.Option(None, "--reason"),
      format_: str = typer.Option("table", "--format"),
      project: Optional[str] = typer.Option(None, "--project", "-p"),
      no_server: bool = typer.Option(False, "--no-server"),
      home: str = typer.Option("", "--home", envvar="SECONDSIGHT_HOME"),
      server_url: str = typer.Option("http://127.0.0.1:8420",
                                      "--server-url",
                                      envvar="SECONDSIGHT_SERVER_URL"),
  ) -> None: ...
  ```
- [ ] Step 4: Mode resolution: exactly one of `{active, disable, enable}`
  must be set; otherwise typer.BadParameter with helpful message.
- [ ] Step 5: For `--disable`, validate `reason` is present and non-empty;
  for `--enable`, validate `reason` is absent (mirror PATCH lifecycle
  rules).
- [ ] Step 6: Server-mode dispatcher: `httpx.get` /
  `httpx.patch`; handle `ConnectError` (silent fallback) and
  `HTTPStatusError` (loud exit) per cli/analyze.py precedent.
- [ ] Step 7: In-process dispatcher: build `DBEngine`, instantiate
  `DirectivesRepository`, call `get_active_conventions` /
  `update_status` / `get_by_id` directly. Wrap each Directive in
  `DirectiveOut.model_validate(...)`.
- [ ] Step 8: Output rendering:
  - `--format json` → `json.dumps([d.model_dump() for d in directives],
    sort_keys=True, default=str)` — and the SAME serialization is
    used by both server-mode and no-server paths.
  - default (table) → Rich table.
- [ ] Step 9: For `--enable` no-op (current already active): print
  "no change — directive {id} is already active" to stderr, exit 0.
- [ ] Step 10: Register in `cli/app.py`:
  `app.add_typer(directive_module.app, name="directive")` — task-5
  will wire this if not already.
- [ ] Step 11: Run tests — all pass.
- [ ] Step 12: Scar report.
- [ ] Step 13: Commit `GUR-104 task-4: cli/directive.py`.

## Expected Scar Report Items

- The byte-identical JSON test (DT-4.1) is hard to keep stable
  unless both code paths use a single serialization function. Don't
  format-shop in either branch.
- httpx.HTTPStatusError vs httpx.ConnectError handling: the order
  matters. Catch ConnectError first (it's a subclass-cousin, not a
  child of HTTPStatusError, but be precise).
- The test for DT-4.1 needs a real server on a random port — use
  `uvicorn.Config + Server` async fixture, or pytest-fastapi-asyncio
  if already installed; otherwise spawn a subprocess. Document the
  approach in the scar.
- Rich table output should be deterministic enough for HP-4.2 to
  assert on; if Rich's box style changes between versions, the
  test breaks. Pin assertion to columns + cell content, not visual
  glyphs.
- `--enable` no-op: the in-process path discovers via `get_by_id`,
  not via `update_status` (which raises if you call it with the
  wrong reason rule). Branch BEFORE calling update_status.

## Acceptance Criteria

Covers `acceptance.yaml`:
- "Silent failure - CLI --no-server JSON drifts from API JSON" (DC-4)
- "Degradation - server-mode CLI fallback to in-process on ConnectError"
- "Degradation - HTTPStatusError does NOT silent-fallback"
- "Success - CLI --active --format json shape matches API"
