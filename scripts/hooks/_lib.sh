#!/usr/bin/env bash
# scripts/hooks/_lib.sh — shared helpers for SecondSight hook scripts.
#
# Source this file from per-event hook scripts.  Provides:
#
#   secondsight_post EVENT_TYPE PAYLOAD_JSON
#     - POSTs to http://127.0.0.1:${SECONDSIGHT_PORT:-8420}/hook/{EVENT_TYPE}
#       with --connect-timeout 0.1 and --max-time 1.
#     - On ANY non-zero exit (curl missing, connection refused, timeout, 5xx):
#         calls secondsight_fallback_append and returns 0 ALWAYS.
#     - Honors $SECONDSIGHT_HOME (default: $HOME/.secondsight)
#     - Honors $SECONDSIGHT_PORT (default: 8420)
#     - Honors $SECONDSIGHT_AGENT (default: "unknown")
#
#   secondsight_fallback_append EVENT_TYPE PAYLOAD_JSON
#     - Builds an envelope wrapper (via jq for safe JSON construction):
#         {"agent":"$SECONDSIGHT_AGENT", "event_type":"...",
#          "timestamp":"...", "payload":{...},
#          "hook_script_version":"<_SECONDSIGHT_VERSION>"}
#     - Atomic-appends one line to $SECONDSIGHT_HOME/fallback_events.jsonl
#     - Uses flock(1) where available (Linux util-linux).
#     - Where flock(1) is unavailable from PATH (notably macOS, but also any
#       minimal/Alpine-style container without util-linux), degrades to plain >>.
#       The PIPE_BUF risk for >512-byte writes applies in the degraded case
#       regardless of OS.
#
# IMPORTANT: This file must NOT set -e.  A non-zero exit from a hook script
# would cancel the agent's tool call (Claude Code PreToolUse contract).
# We manage exit status explicitly: every function returns 0.
#
# Assumption: PAYLOAD_JSON is valid JSON (the hook receives JSON from the agent).
# If jq is absent AND curl is absent, the event is written to JSONL with a
# degraded stub that preserves payload byte-count and a truncated base64 snapshot.
#
# hook_script_version: phase-1.2

# Do NOT set -e here.  Hooks must never exit non-zero.
set -u

# ---------------------------------------------------------------------------
# Version constant (I1 fix: single source of truth)
# ---------------------------------------------------------------------------
# Bump this when the envelope schema or _lib.sh behavior changes.
# Referenced by both jq-present path and jq-absent (degraded) path below.
readonly _SECONDSIGHT_VERSION="phase-1.2"

# ---------------------------------------------------------------------------
# Resolve SECONDSIGHT_HOME
# ---------------------------------------------------------------------------

_secondsight_resolve_home() {
    # Validate that SECONDSIGHT_HOME is either unset (use default) or absolute.
    # A relative path would be resolved against the hook's working directory,
    # which is unpredictable in an agent context.
    local home_val="${SECONDSIGHT_HOME:-}"

    if [ -z "$home_val" ]; then
        # Default: $HOME/.secondsight
        # shellcheck disable=SC2154
        printf '%s/.secondsight' "${HOME:-/tmp}"
        return 0
    fi

    # Check if it starts with /
    case "$home_val" in
        /*)
            printf '%s' "$home_val"
            return 0
            ;;
        *)
            printf 'secondsight_warning: SECONDSIGHT_HOME=%s is not absolute; ignoring, using default %s/.secondsight\n' \
                "$home_val" "${HOME:-/tmp}" >&2
            printf '%s/.secondsight' "${HOME:-/tmp}"
            return 0
            ;;
    esac
}

# ---------------------------------------------------------------------------
# secondsight_fallback_append EVENT_TYPE PAYLOAD_JSON
# ---------------------------------------------------------------------------

secondsight_fallback_append() {
    local event_type="$1"
    local payload_json="$2"

    local ss_home
    ss_home="$(_secondsight_resolve_home)"

    # Auto-create the home directory if it does not exist.
    if ! mkdir -p "$ss_home" 2>/dev/null; then
        printf 'secondsight_warning: cannot create SECONDSIGHT_HOME=%s; event lost.\n' \
            "$ss_home" >&2
        return 0
    fi

    local fallback_file="$ss_home/fallback_events.jsonl"
    local agent="${SECONDSIGHT_AGENT:-unknown}"
    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || printf 'unknown')"

    # Build the envelope using jq for safe JSON construction.
    # jq handles all shell metacharacters in payload_json correctly.
    # We extract .payload from the incoming hook envelope so the fallback
    # wraps only the tool-specific payload (not the entire outer envelope).
    # This keeps the fallback envelope shape consistent with what the server
    # would have received in the POST body's payload field.
    if command -v jq > /dev/null 2>&1; then
        local envelope
        envelope="$(
            jq -c -n \
                --arg agent "$agent" \
                --arg event_type "$event_type" \
                --arg timestamp "$ts" \
                --arg version "$_SECONDSIGHT_VERSION" \
                --argjson full_payload "$payload_json" \
                '{
                    agent: $agent,
                    event_type: $event_type,
                    timestamp: $timestamp,
                    payload: ($full_payload.payload // $full_payload),
                    hook_script_version: $version
                }' 2>/dev/null
        )"
        if [ -z "$envelope" ]; then
            # jq failed (invalid payload_json?) — emit a warning, do not crash.
            printf 'secondsight_warning: jq failed to construct envelope for event_type=%s; event lost.\n' \
                "$event_type" >&2
            return 0
        fi
    else
        # jq is absent — construct a degraded envelope with payload metadata (C4 fix).
        # We cannot safely embed arbitrary JSON without jq.  Instead we preserve:
        #   _original_payload_bytes: byte count of the original payload
        #   _original_payload_b64_truncated: base64 of the first 4096 bytes
        #     (bounded to keep JSONL lines manageable; '(unavailable)' if base64
        #     is also absent from PATH).
        # This allows operators to distinguish "hook invoked with empty payload"
        # from "hook invoked with real payload that was truncated due to jq absence".
        printf 'secondsight_warning: jq not found; storing degraded envelope for event_type=%s.\n' \
            "$event_type" >&2

        local _payload_bytes
        _payload_bytes=$(printf '%s' "$payload_json" | wc -c | tr -d ' ')

        local _payload_b64_truncated
        _payload_b64_truncated="(unavailable)"
        if command -v base64 > /dev/null 2>&1; then
            # Truncate to first 4096 bytes to keep JSONL lines bounded.
            _payload_b64_truncated=$(printf '%s' "$payload_json" | head -c 4096 | base64 | tr -d '\n')
        fi

        local envelope
        # Use _SECONDSIGHT_VERSION for the version field (I1 fix).
        # shellcheck disable=SC2016
        envelope="{\"agent\":\"${agent}\",\"event_type\":\"${event_type}\",\"timestamp\":\"${ts}\",\"payload\":{\"_fallback_degraded\":true,\"_original_payload_bytes\":${_payload_bytes},\"_original_payload_b64_truncated\":\"${_payload_b64_truncated}\"},\"hook_script_version\":\"${_SECONDSIGHT_VERSION}\"}"
    fi

    # Atomic append: use flock if available.
    # Where flock(1) is unavailable from PATH (notably macOS, but also any
    # minimal/Alpine-style container without util-linux), degrade to plain >>.
    # The PIPE_BUF risk for >512-byte writes applies in the degraded case
    # regardless of OS.  This is documented and accepted for Phase 1.
    if command -v flock > /dev/null 2>&1; then
        # flock -x: exclusive lock on the fallback file's lock file.
        # The lock file is separate from the data file so the lock fd
        # does not interfere with the append.
        local lock_file="${fallback_file}.lock"
        (
            flock -x 9
            printf '%s\n' "$envelope" >> "$fallback_file"
        ) 9>"$lock_file" 2>/dev/null || {
            # flock or append failed — try plain >>
            printf '%s\n' "$envelope" >> "$fallback_file" 2>/dev/null || true
        }
    else
        # No flock — plain >> (PIPE_BUF interleaving risk for large payloads).
        printf '%s\n' "$envelope" >> "$fallback_file" 2>/dev/null || true
    fi

    return 0
}

# ---------------------------------------------------------------------------
# secondsight_post EVENT_TYPE PAYLOAD_JSON
# ---------------------------------------------------------------------------

secondsight_post() {
    local event_type="$1"
    local payload_json="$2"

    # Guard: if payload is empty (stdin not connected, or hook called without
    # input), emit a warning and skip — we cannot build a valid envelope from
    # an empty string. This prevents jq from failing on "" input.
    if [ -z "$payload_json" ]; then
        printf 'secondsight_warning: empty payload for event_type=%s; event skipped.\n' \
            "$event_type" >&2
        return 0
    fi

    local port="${SECONDSIGHT_PORT:-8420}"
    local url="http://127.0.0.1:${port}/hook/${event_type}"

    # Check if curl is available before attempting.
    if ! command -v curl > /dev/null 2>&1; then
        printf 'secondsight_warning: curl not found; falling back to JSONL for event_type=%s.\n' \
            "$event_type" >&2
        secondsight_fallback_append "$event_type" "$payload_json"
        return 0
    fi

    # Auto-create logs directory for curl diagnostic trail (I6 fix).
    local ss_home
    ss_home="$(_secondsight_resolve_home)"
    local logs_dir="$ss_home/logs"
    mkdir -p "$logs_dir" 2>/dev/null || true
    local curl_error_log="$logs_dir/curl-errors.log"

    # POST with tight timeouts so the hook does not block the agent.
    # --connect-timeout 0.1: fail fast if server is not listening.
    # --max-time 1: hard cap on total request time.
    # -s: silent (no progress bar).
    # -o /dev/null: discard response body.
    # -w "%{http_code}": capture HTTP status code.
    # stderr redirected to a single appended log file at $ss_home/logs/curl-errors.log
    # (I6 fix). NO date rotation in Phase 1: the file grows unbounded; operators
    # should rotate externally (logrotate / launchd) or truncate manually. Tracked
    # as KS for Phase 2. The win versus the original 2>/dev/null: a proxy/TLS/DNS
    # failure is now distinguishable from "server not running" in operator logs.
    local http_code
    http_code="$(
        curl \
            --silent \
            --connect-timeout 0.1 \
            --max-time 1 \
            --request POST \
            --header 'Content-Type: application/json' \
            --data "$payload_json" \
            --output /dev/null \
            --write-out '%{http_code}' \
            "$url" \
            2>>"$curl_error_log"
    )" || http_code="000"

    # Treat any non-2xx as a failure → fallback.
    # 000 = curl connection error (server not running, timeout, etc.)
    case "$http_code" in
        2*)
            # Success: server received the event.
            return 0
            ;;
        *)
            # Failure: server is down, returned 4xx/5xx, or curl timed out.
            secondsight_fallback_append "$event_type" "$payload_json"
            return 0
            ;;
    esac
}
