# SecondSight API

SecondSight exposes a local FastAPI server. With the default config:

```text
http://127.0.0.1:8420
```

FastAPI's interactive docs are also available while the server is running:

```text
http://127.0.0.1:8420/docs
```

Most read APIs are project-scoped and require `project_id`.

## Health

```http
GET /health
```

Returns server liveness, package version, and uptime.

## Hook Ingestion

Preferred thin ingress route:

```http
POST /hook/{agent}/{event_type}
```

Example:

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

Legacy compatibility route:

```http
POST /hook/{event_type}
```

Supported event types include:

- `session_start`
- `session_end`
- `user_prompt`
- `thinking`
- `tool_use_start`
- `tool_use_end`
- `sub_agent_start`
- `sub_agent_end`
- `task_created`
- `task_completed`
- `response`

## Hook Injection

```http
POST /hook/injection/session-start/{agent}
```

Returns agent-native SessionStart output when active directives are available,
or `204` when there is nothing to inject.

## Observation

```http
GET /api/sessions?project_id=<project_id>
GET /api/sessions/{session_id}?project_id=<project_id>
GET /api/sessions/{session_id}/segments?project_id=<project_id>
GET /api/sessions/{session_id}/segments/{segment_index}?project_id=<project_id>
```

Session list pagination supports `limit`, `offset`, and `cursor`.

## Analysis Reads

```http
GET /api/analysis/summary?project_id=<project_id>
GET /api/analysis/sessions?project_id=<project_id>
GET /api/analysis/sessions/{session_id}?project_id=<project_id>
GET /api/analysis/sessions/{session_id}/flags?project_id=<project_id>
GET /api/analysis/trends?project_id=<project_id>
GET /api/analysis/aggregation?project_id=<project_id>
```

These endpoints read persisted analysis output. To create or retry analysis
from the CLI, use:

```bash
secondsight analyze --project <project_id> --session <session_id> --no-server
```

## Directives

List directives:

```http
GET /api/directives?project_id=<project_id>
GET /api/directives?project_id=<project_id>&active=false
```

List revision history:

```http
GET /api/directives/{directive_id}/revisions?project_id=<project_id>
```

Update lifecycle status:

```http
PATCH /api/directives/{directive_id}?project_id=<project_id>
```

Disable:

```bash
curl -X PATCH \
  'http://127.0.0.1:8420/api/directives/<directive_id>?project_id=<project_id>' \
  -H 'Content-Type: application/json' \
  -d '{"status":"disabled","reason":"obsolete guidance"}'
```

Re-enable:

```bash
curl -X PATCH \
  'http://127.0.0.1:8420/api/directives/<directive_id>?project_id=<project_id>' \
  -H 'Content-Type: application/json' \
  -d '{"status":"active"}'
```

## Caching

Observation, analysis, and directive list endpoints use weak ETags where the
underlying repository can compute one. Clients may send `If-None-Match` and
receive `304` when data has not changed.
