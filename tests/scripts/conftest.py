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
EXPECTED_VERSION: str = "phase-1.2"

# I2: fallback file name — referenced by every test instead of bare string.
FALLBACK_FILENAME: str = "fallback_events.jsonl"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = (
    Path(__file__).parent.parent.parent / "scripts" / "hooks"
)


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

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
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

# Test stub: a standalone Normalizer that DELEGATES to IdentityNormalizer for
# the normalize() body and adds an agent="claude-code" gate in supports(). We
# use composition (not inheritance) so the Normalizer Protocol contract is the
# only structural surface. Two coupling risks remain and must be checked when
# either changes:
#   1. Behavioral coupling — if IdentityNormalizer.normalize() ever does
#      anything other than identity pass-through, UT-1 / UT-1b silently change
#      meaning. IdentityNormalizer is a documented test/baseline normalizer; a
#      contract change is a Phase 2+ event.
#   2. Signature coupling — the delegation call at normalize() below uses the
#      exact (envelope, event_type) positional shape. If IdentityNormalizer
#      gains, removes, or reorders parameters, this fixture breaks at runtime
#      (NOT at import). Mitigation: keep the Protocol's signature stable; if it
#      does evolve, update this fixture in the same change.
class _ClaudeCodeNormalizer:
    """Accept agent='claude-code' for all canonical EventType values.

    Test-only stub: same pass-through logic as IdentityNormalizer but
    scoped to agent='claude-code'.  The real ClaudeCode adapter (P1-9)
    will replace this with semantic field mapping.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        if agent != "claude-code":
            return False
        from secondsight.event import EventType
        try:
            EventType(event_type)
            return True
        except ValueError:
            return False

    def normalize(self, envelope: Any, event_type: str) -> Any:
        from secondsight.api.normalizer import IdentityNormalizer
        return IdentityNormalizer().normalize(envelope, event_type)


@pytest.fixture()
def real_secondsight_server(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Start a real create_app() server on a kernel-assigned port.

    NC1 fix: uses port=0 (kernel assigns) to eliminate the TOCTOU race in
    the old _find_free_port() approach. The kernel holds the binding from
    the moment uvicorn calls bind() — there is no window between port
    discovery and port binding during which another process could steal the port.

    C2 fix: this tests the actual FastAPI route stack including EventType enum
    validation, IdentityNormalizer dispatch, SessionTracker.bind(), and
    pipeline.ingest() — not a fake HTTP handler that accepts any URL.

    The _ClaudeCodeNormalizer (registered here) handles agent="claude-code".
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

    # Inject test normalizer into the registry's _normalizers list.
    # This is a cross-thread mutation: main thread (this fixture) appends; the
    # uvicorn worker thread reads on every request. Safety holds under CPython
    # only — the GIL makes list.append atomic, so the reader never observes a
    # torn write. A free-threaded Python build (PEP 703) would invalidate this.
    # See NormalizerRegistry implementation for the read-side path.
    app.state.server_state.normalizer_registry.register(_ClaudeCodeNormalizer())

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
    agent: str = "test-agent",
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
