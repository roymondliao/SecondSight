"""Shared path-component safety check for project_id and session_id.

Hardening for GUR-147 security review: HIGH-1 (Observation API path
traversal) and MEDIUM-1 (cleanup CLI ``--project-id``) collapsed onto one
helper so all entry points (hooks router, observation router, cleanup CLI)
apply the same rule.

Why this lives in ``api/`` rather than ``_common/``: the validator's reach
today is the HTTP/CLI input boundary plus storage callers that consume
those inputs. Promoting to ``_common`` would suggest broader reuse than
warranted; revisit if a future component outside the request path needs
the same check.

Rejects:
- Empty string (defensive; pydantic ``min_length=1`` should catch first).
- Path-traversal characters: ``/``, ``\\``, null byte.
- ASCII control chars (``\\x00``–``\\x1f``, ``\\x7f``) and whitespace
  (``\\t``, ``\\n``, ``\\r``) that some filesystems strip silently.
- Pure-dot sequences (``.``, ``..``) used as traversal shorthand.

Dots in other positions (e.g. ``com.company.project``) are allowed —
matches the Phase 1 KS-4 decision the hooks router has been honouring.
"""

from __future__ import annotations

_UNSAFE_ID_CHARS = frozenset("/\\\x00\t\n\r" + "".join(chr(c) for c in range(0x01, 0x20)) + "\x7f")


def is_safe_id(value: str) -> bool:
    """Return True if ``value`` is safe to use as a path component."""
    if not value:
        return False
    if _UNSAFE_ID_CHARS.intersection(value):
        return False
    if all(c == "." for c in value):
        return False
    return True


__all__ = ["is_safe_id"]
