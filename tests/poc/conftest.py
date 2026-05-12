"""
Sample event fixtures for testing the Unified Event Schema.

These fixtures are derived from actual event data discovered during
Tasks 1-3 investigation, including:
- Claude Code: hook payloads + JSONL transcript records
- OpenCode: plugin API events + SQLite DB records
- Codex: JSONL rollout file records + hook callback payloads

Reference sources:
- reference_opensoure/lazyagent/internal/codex/process_test.go (synthetic JSONL)
- reference_opensoure/observagent/hooks/relay.py (live payload fields)
- investigations/claude-code-hooks.yaml, opencode-hooks.yaml, codex-hooks.yaml
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Claude Code sample events (from Task 1 investigation)
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_code_pre_tool_use_event() -> dict:
    """PreToolUse hook payload - confirmed from observagent relay.py."""
    return {
        "agent": "claude_code",
        "raw": {
            "session_id": "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "toolu_01XYZ123456",
            "tool_input": {
                "command": "git status",
            },
            "transcript_path": "/Users/dev/.claude/projects/-Users-dev-myapp/fa493ff8.jsonl",
            "cwd": "/Users/dev/myapp",
            "permission_mode": "default",
        },
    }


@pytest.fixture
def claude_code_post_tool_use_event() -> dict:
    """PostToolUse hook payload - confirmed from observagent relay.py."""
    return {
        "agent": "claude_code",
        "raw": {
            "session_id": "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "toolu_01XYZ123456",
            "tool_input": {
                "command": "git status",
            },
            "tool_response": {
                "stdout": "On branch main\nnothing to commit",
                "stderr": "",
            },
            "transcript_path": "/Users/dev/.claude/projects/-Users-dev-myapp/fa493ff8.jsonl",
            "cwd": "/Users/dev/myapp",
        },
    }


@pytest.fixture
def claude_code_stop_event() -> dict:
    """Stop hook payload - confirmed from langfuse-template."""
    return {
        "agent": "claude_code",
        "raw": {
            "session_id": "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0",
            "hook_event_name": "Stop",
            "transcript_path": "/Users/dev/.claude/projects/-Users-dev-myapp/fa493ff8.jsonl",
            "stop_hook_active": False,
        },
    }


@pytest.fixture
def claude_code_jsonl_assistant_event() -> dict:
    """JSONL transcript assistant message - confirmed from live file inspection."""
    return {
        "agent": "claude_code",
        "raw": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll check the git status."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git status"},
                        "id": "toolu_01XYZ123456",
                    },
                ],
                "model": "claude-sonnet-4-6",
                "id": "msg_01ABC",
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 0,
                },
                "stop_reason": "tool_use",
            },
            "sessionId": "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0",
            "timestamp": "2026-04-24T10:30:00.000Z",
            "cwd": "/Users/dev/myapp",
            "version": "2.1.85",
            "uuid": "uuid-msg-001",
            "parentUuid": None,
        },
    }


@pytest.fixture
def claude_code_subagent_start_event() -> dict:
    """SubagentStart hook payload - confirmed from observagent cmd-init.js."""
    return {
        "agent": "claude_code",
        "raw": {
            "session_id": "fa493ff8-3856-4a1b-9c2d-e5f6a7b8c9d0",
            "hook_event_name": "SubagentStart",
            "agent_id": "agent-subagent-001",
            "agent_type": "claude",
            "agent_transcript_path": "/Users/dev/.claude/projects/-Users-dev-myapp/fa493ff8/subagents/agent-subagent-001.jsonl",
        },
    }


# ---------------------------------------------------------------------------
# OpenCode sample events (from Task 2 investigation)
# ---------------------------------------------------------------------------


@pytest.fixture
def opencode_tool_execute_before_event() -> dict:
    """tool.execute.before plugin hook - confirmed from plugin API docs."""
    return {
        "agent": "opencode",
        "raw": {
            "input": {
                "tool": "bash",
                "sessionID": "8c5d2e1a-f4b6-4c7d-8e9f-0a1b2c3d4e5f",
                "callID": "call-oc-001",
            },
            "output": {
                "args": {"command": "ls -la"},
            },
        },
    }


@pytest.fixture
def opencode_tool_execute_after_event() -> dict:
    """tool.execute.after plugin hook - confirmed from plugin API docs."""
    return {
        "agent": "opencode",
        "raw": {
            "input": {
                "tool": "bash",
                "sessionID": "8c5d2e1a-f4b6-4c7d-8e9f-0a1b2c3d4e5f",
                "callID": "call-oc-001",
                "args": {"command": "ls -la"},
            },
            "output": {
                "output": "total 64\ndrwxr-xr-x  10 dev  staff   320 Apr 24 10:30 .",
                "title": "bash: ls -la",
                "metadata": {"exit_code": 0},
            },
        },
    }


@pytest.fixture
def opencode_session_created_event() -> dict:
    """event.session.created event - from plugin API docs."""
    return {
        "agent": "opencode",
        "raw": {
            "event": {
                "properties": {
                    "sessionID": "8c5d2e1a-f4b6-4c7d-8e9f-0a1b2c3d4e5f",
                },
            },
        },
    }


@pytest.fixture
def opencode_db_message_event() -> dict:
    """SQLite message table record - confirmed from lazyagent cross-validation."""
    return {
        "agent": "opencode",
        "raw": {
            "source": "db_polling",
            "message": {
                "data": {
                    "role": "assistant",
                    "cost": 0.0042,
                    "tokens": {
                        "input": 2000,
                        "output": 150,
                        "cache": {
                            "read": 800,
                            "write": 200,
                        },
                    },
                    "time": {
                        "created": 1714000200000,
                    },
                },
            },
            "session_id": "8c5d2e1a-f4b6-4c7d-8e9f-0a1b2c3d4e5f",
        },
    }


@pytest.fixture
def opencode_db_part_tool_event() -> dict:
    """SQLite part table tool record - confirmed from lazyagent cross-validation."""
    return {
        "agent": "opencode",
        "raw": {
            "source": "db_polling",
            "part": {
                "data": {
                    "type": "tool",
                    "tool": "read",
                    "callID": "call-oc-002",
                    "state": {
                        "input": {"path": "/Users/dev/myapp/src/main.py"},
                        "output": "def main():\n    print('hello')",
                        "time": {
                            "start": 1714000100000,
                            "end": 1714000101500,
                        },
                    },
                },
            },
            "session_id": "8c5d2e1a-f4b6-4c7d-8e9f-0a1b2c3d4e5f",
        },
    }


# ---------------------------------------------------------------------------
# Codex sample events (from Task 3 investigation)
# ---------------------------------------------------------------------------


@pytest.fixture
def codex_session_meta_event() -> dict:
    """session_meta JSONL event - confirmed from lazyagent process_test.go."""
    return {
        "agent": "codex",
        "raw": {
            "timestamp": "2026-03-28T11:26:17.785Z",
            "type": "session_meta",
            "payload": {
                "id": "019d3431-8669-7603-be71-7079fa555f4a",
                "cwd": "/tmp/project",
                "cli_version": "0.116.0",
                "source": "cli",
            },
        },
    }


@pytest.fixture
def codex_function_call_event() -> dict:
    """response_item/function_call JSONL event - confirmed from lazyagent process_test.go.

    CRITICAL: arguments is a JSON-ENCODED STRING, not a dict.
    This is the double-parsing case noted in the cross-task consistency notes.
    """
    return {
        "agent": "codex",
        "raw": {
            "timestamp": "2026-03-28T11:26:19.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"rg codex"}',
                "call_id": "call_codex_001",
            },
        },
    }


@pytest.fixture
def codex_function_call_output_event() -> dict:
    """response_item/function_call_output JSONL event."""
    return {
        "agent": "codex",
        "raw": {
            "timestamp": "2026-03-28T11:26:20.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_codex_001",
                "output": "ok",
            },
        },
    }


@pytest.fixture
def codex_token_count_event() -> dict:
    """event_msg/token_count JSONL event - confirmed from lazyagent process_test.go."""
    return {
        "agent": "codex",
        "raw": {
            "timestamp": "2026-03-28T11:26:21.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 300,
                        "output_tokens": 500,
                        "reasoning_output_tokens": 100,
                    },
                    "last_token_usage": {
                        "input_tokens": 400,
                        "cached_input_tokens": 100,
                        "output_tokens": 150,
                        "reasoning_output_tokens": 30,
                    },
                },
            },
        },
    }


@pytest.fixture
def codex_user_message_event() -> dict:
    """response_item/message (role=user) JSONL event."""
    return {
        "agent": "codex",
        "raw": {
            "timestamp": "2026-03-28T11:26:18.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "please add codex"},
                ],
            },
        },
    }


@pytest.fixture
def codex_post_tool_use_hook_event() -> dict:
    """post_tool_use hook callback payload - confirmed from codex-rs types.rs."""
    return {
        "agent": "codex",
        "raw": {
            "session_id": "019d3431-8669-7603-be71-7079fa555f4a",
            "cwd": "/tmp/project",
            "triggered_at": "2026-03-28T11:26:20.500Z",
            "hook_event": {
                "event_type": "after_tool_use",
                "turn_id": "turn-001",
                "call_id": "call_codex_001",
                "tool_name": "exec_command",
                "tool_kind": "function",
                "tool_input": {"arguments": '{"cmd":"rg codex"}'},
                "executed": True,
                "success": True,
                "duration_ms": 1500,
                "mutating": False,
                "sandbox": "read-only",
                "output_preview": "ok",
            },
        },
    }


@pytest.fixture
def codex_task_complete_event() -> dict:
    """event_msg/task_complete JSONL event."""
    return {
        "agent": "codex",
        "raw": {
            "timestamp": "2026-03-28T11:26:22.000Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-001",
                "completed_at": 1711618582,
                "duration_ms": 5000,
            },
        },
    }


# ---------------------------------------------------------------------------
# Pathological / edge case fixtures for death tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Storage fixtures (Task 8)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_codex_jsonl_events() -> list[dict]:
    """Real Codex JSONL events from reference_opensoure/lazyagent/internal/codex/process_test.go.

    These represent a complete session with:
    - session_meta
    - turn_context
    - user message
    - function_call (with JSON-encoded string arguments)
    - function_call_output
    - token_count
    - assistant message
    """
    session_id = "019d3431-8669-7603-be71-7079fa555f4a"
    return [
        {
            "timestamp": "2026-03-28T11:26:17.785Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/tmp/project",
                "cli_version": "0.116.0",
                "source": "cli",
            },
        },
        {
            "timestamp": "2026-03-28T11:26:17.900Z",
            "type": "turn_context",
            "payload": {
                "cwd": "/tmp/project",
                "model": "gpt-5.2-codex",
                "git": {"branch": "main"},
            },
        },
        {
            "timestamp": "2026-03-28T11:26:18.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "please add codex"}],
            },
        },
        {
            "timestamp": "2026-03-28T11:26:19.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"rg codex"}',
            },
        },
        {
            "timestamp": "2026-03-28T11:26:20.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "ok",
            },
        },
        {
            "timestamp": "2026-03-28T11:26:21.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 300,
                        "output_tokens": 500,
                        "reasoning_output_tokens": 100,
                    },
                },
            },
        },
        {
            "timestamp": "2026-03-28T11:26:22.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "implemented"}],
            },
        },
    ]


@pytest.fixture
def large_tool_result_payload() -> str:
    """A realistic large tool result (~100KB).

    Simulates a Bash tool returning a large directory listing or file contents.
    Real-world: `find . -type f` on a medium-sized project, or `cat` of a
    large source file.
    """
    # Typical file listing line: "drwxr-xr-x  10 dev  staff   320 Apr 24 10:30 src/components/Button.tsx\n"
    lines = []
    for i in range(2000):
        lines.append(
            f"-rw-r--r--  1 dev  staff  {i * 100:>8d} Apr 24 10:{i % 60:02d} "
            f"src/module_{i:04d}/component_{i:04d}.tsx"
        )
    return "\n".join(lines)
