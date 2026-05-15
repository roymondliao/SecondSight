"""Shared fixtures for bash hook script tests (task-4)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest


# ---------------------------------------------------------------------------
# Constants (I1, I2 — single source of truth for version and filenames)
# ---------------------------------------------------------------------------

# I1: version string from _lib.sh — referenced by every assertion so
#     a version bump only requires changing one place.
EXPECTED_VERSION: str = "phase-2.0"

# I2: fallback file name — referenced by every test instead of bare string.
FALLBACK_FILENAME: str = "fallback_events.jsonl"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).parent.parent.parent / "scripts" / "hooks"


def hook_script(name: str) -> Path:
    """Return the absolute path to a hook script by name."""
    return HOOKS_DIR / name


def run_hook(
    script: Path,
    payload: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    """Run a hook script with the given JSON payload on stdin.

    Returns the CompletedProcess — callers inspect returncode, stdout, stderr.
    """
    return subprocess.run(
        ["/usr/bin/env", "bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Fake HTTP server fixture (returns configurable status code)
# ---------------------------------------------------------------------------


class _FakeHandler(BaseHTTPRequestHandler):
    """Request handler that returns a configurable status code and JSON body."""

    _status_code: int = 200

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _body = self.rfile.read(length)
        self.send_response(self._status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps({"status": "ok"}).encode()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002,D102
        pass  # suppress default request logging in test output


def _make_handler_class(status_code: int) -> type:
    """Return a subclass of _FakeHandler bound to a specific status code."""

    class _Handler(_FakeHandler):
        _status_code = status_code

    return _Handler


@pytest.fixture()
def fake_server_200() -> Iterator[int]:
    """Fake HTTP server returning 200 on all POSTs.  Yields the port number."""
    server = HTTPServer(("127.0.0.1", 0), _make_handler_class(200))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


@pytest.fixture()
def fake_server_500() -> Iterator[int]:
    """Fake HTTP server returning 500 on all POSTs.  Yields the port number."""
    server = HTTPServer(("127.0.0.1", 0), _make_handler_class(500))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


# ---------------------------------------------------------------------------
# Real create_app() server fixture (C2 — for true integration tests)
# ---------------------------------------------------------------------------


# Test stub: a standalone AgentAdapter subclass that DELEGATES to
# IdentityAdapter for the normalize() body and adds an agent="claude_code"
# gate in supports(). Composition (not inheritance from IdentityAdapter) keeps
# the AgentAdapter ABC contract — and only the ABC contract — as the structural
# surface. Two coupling risks remain and must be checked when either changes:
#   1. Behavioural coupling — if IdentityAdapter.normalize() ever does
#      anything other than identity pass-through, UT-1 / UT-1b silently change
#      meaning. IdentityAdapter is a documented test/baseline adapter; a
#      contract change is a Phase 2+ event.
#   2. Signature coupling — the delegation call at normalize() below uses the
#      exact (envelope, event_type) positional shape. If IdentityAdapter
#      gains, removes, or reorders parameters, this fixture breaks at runtime
#      (NOT at import). Mitigation: keep the AgentAdapter signature stable;
#      if it does evolve, update this fixture in the same change.
#
# Note: this stub is replaced by the real ClaudeCodeAdapter in task-4 of
# phase1-adapters (GUR-124). Keep the supports()/supported_event_types()
# scope identical to ease the swap.
class _ClaudeCodeAdapterStub:
    """Test-only AgentAdapter stub for agent='claude_code' pass-through.

    Subclasses AgentAdapter at registration time via duck-cast. The real
    ClaudeCode adapter (P1-10 / GUR-124) will replace this with semantic
    field mapping.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        if agent != "claude_code":
            return False
        from secondsight.event import EventType

        try:
            EventType(event_type)
            return True
        except ValueError:
            return False

    def normalize(self, envelope: Any, event_type: str) -> Any:
        from secondsight.adapters import IdentityAdapter

        return IdentityAdapter().normalize(envelope, event_type)

    def supported_event_types(self) -> set[str]:
        # DT-6 alignment: every event_type that supports("claude_code", *)
        # answers True for must appear here, otherwise AdapterRegistry.for_()
        # rejects this stub via the consistency guard. The full EventType set
        # is the universal-test floor — task-4 narrows this for the real adapter.
        from secondsight.event import EventType

        return {e.value for e in EventType}

    # NotImplementedError defaults for inject_convention / inject_hint:
    # the stub is duck-cast into the AdapterRegistry below, so register()
    # does not enforce ABC subclassing. Tests that drive the route to the
    # injection seams must replace this stub. For now, raising loudly is
    # the documented escape hatch.
    def inject_convention(self, convention: Any) -> str:
        raise NotImplementedError(
            "_ClaudeCodeAdapterStub does not implement inject_convention; "
            "use the real ClaudeCodeAdapter (GUR-124) for injection-path tests."
        )

    def inject_hint(self, hint: Any) -> str:
        return ""


@pytest.fixture()
def real_secondsight_server(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Start a real create_app() server on a kernel-assigned port.

    NC1 fix: uses port=0 (kernel assigns) to eliminate the TOCTOU race in
    the old _find_free_port() approach. The kernel holds the binding from
    the moment uvicorn calls bind() — there is no window between port
    discovery and port binding during which another process could steal the port.

    C2 fix: this tests the actual FastAPI route stack including EventType enum
    validation, IdentityAdapter dispatch, SessionTracker.bind(), and
    pipeline.ingest() — not a fake HTTP handler that accepts any URL.

    The _ClaudeCodeAdapterStub (registered here) handles agent="claude_code".
    Yields a dict with:
      - port: int  (kernel-assigned; read back from the bound socket)
      - home: Path (secondsight_home)
      - project_id: str
      - session_id: str

    The server runs in a daemon thread and is shut down via uvicorn.Server.should_exit.
    """
    import uvicorn

    from secondsight.api.server import create_app

    home = tmp_path / ".secondsight"
    home.mkdir(parents=True, exist_ok=True)

    app = create_app(secondsight_home=home)

    # port=0: kernel assigns a free port atomically at bind() time.
    # This eliminates the TOCTOU race in _find_free_port() (NC1 fix):
    # no other process can steal the port between discovery and binding.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to bind and expose its port.
    # server.started is set by uvicorn after the socket is bound and listening.
    # server.servers[0].sockets[0].getsockname() reveals the kernel-assigned port.
    deadline = time.monotonic() + 5.0
    host: str = "127.0.0.1"
    port: int = 0
    while time.monotonic() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            sock = server.servers[0].sockets[0]
            host, port = sock.getsockname()[:2]
            break
        time.sleep(0.01)
    else:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError("Real secondsight server failed to start within 5s")

    # Inject test adapter into the registry's _adapters list.
    # This is a cross-thread mutation: main thread (this fixture) appends; the
    # uvicorn worker thread reads on every request. Safety holds under CPython
    # only — the GIL makes list.append atomic, so the reader never observes a
    # torn write. A free-threaded Python build (PEP 703) would invalidate this.
    # See AdapterRegistry implementation for the read-side path.
    # Cast: AdapterRegistry.register() type-hints AgentAdapter, but the duck-
    # typed stub satisfies the runtime contract (supports + normalize +
    # supported_event_types). We bypass mypy here intentionally.
    #
    # Suppress the RuntimeWarning emitted by AdapterRegistry.register() on
    # overlapping supported_event_types(). The IdentityAdapter (registered by
    # the lifespan) publishes the full EventType set scoped to agent="test";
    # this stub publishes the full set scoped to agent="claude_code". The
    # event_type overlap is real but the agent gating prevents any dispatch
    # collision — the documented benign case the warning's docstring calls
    # out. Future readers: if you remove the agent gate on either side, the
    # warning becomes load-bearing again — re-enable it.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        app.state.server_state.adapter_registry.register(_ClaudeCodeAdapterStub())  # type: ignore[arg-type]

    try:
        yield {
            "port": port,
            "home": home,
            "project_id": "proj-test",
            "session_id": "sess-test",
        }
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Environment builder
# ---------------------------------------------------------------------------


def build_env(
    *,
    port: int | str,
    home: Path,
    agent: str = "claude_code",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment dict for running hook scripts.

    Includes PATH (so bash can find jq, curl, etc.), SECONDSIGHT_PORT,
    SECONDSIGHT_HOME, and SECONDSIGHT_AGENT.  Extra key-value pairs are merged.
    """
    import os

    env: dict[str, str] = {
        # Preserve PATH so jq/curl are findable
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "SECONDSIGHT_PORT": str(port),
        "SECONDSIGHT_HOME": str(home),
        "SECONDSIGHT_AGENT": agent,
    }
    if extra:
        env.update(extra)
    return env
