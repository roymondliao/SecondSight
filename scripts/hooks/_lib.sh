#!/usr/bin/env bash
# scripts/hooks/_lib.sh — shared helpers for SecondSight hook scripts.
#
# Source this file from per-event hook scripts.  Provides:
#
#   secondsight_post EVENT_TYPE PAYLOAD_JSON
#     - POSTs to http://127.0.0.1:${SECONDSIGHT_PORT:-8420}/hook/{AGENT}/{EVENT_TYPE}
#       using a thin ingress body:
#         {"event_id","timestamp","sequence_number","payload"}
#       with --connect-timeout 0.1 and --max-time 1.
#     - On ANY non-zero exit (curl missing, connection refused, timeout, 5xx):
#         calls secondsight_fallback_append and returns 0 ALWAYS.
#     - Honors $SECONDSIGHT_HOME (default: $HOME/.secondsight)
#     - Honors $SECONDSIGHT_PORT (default: 8420)
#     - Honors $SECONDSIGHT_AGENT (default: "claude_code")
#
#   secondsight_fallback_append EVENT_TYPE PAYLOAD_JSON
#     - Stores the full ingress replay record:
#         {"agent":"$SECONDSIGHT_AGENT","event_type":"...",
#          "event_id":"...","timestamp":"...","sequence_number":N,
#          "payload":{...},"hook_script_version":"<_SECONDSIGHT_VERSION>"}
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
# hook_script_version: phase-2.0

# Do NOT set -e here.  Hooks must never exit non-zero.
set -u

# ---------------------------------------------------------------------------
# Version constant (I1 fix: single source of truth)
# ---------------------------------------------------------------------------
# Bump this when the envelope schema or _lib.sh behavior changes.
# Referenced by both jq-present path and jq-absent (degraded) path below.
readonly _SECONDSIGHT_VERSION="phase-2.0"

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
    local fallback_record
    fallback_record="$(_secondsight_build_fallback_record "$event_type" "$payload_json")"
    if [ -z "$fallback_record" ]; then
        printf 'secondsight_warning: could not build fallback ingress record for event_type=%s; event skipped.\n' \
            "$event_type" >&2
        return 0
    fi
    _secondsight_fallback_append_record "$fallback_record" "$ss_home"
    return 0
}

_secondsight_fallback_append_record() {
    local fallback_record="$1"
    local ss_home="$2"

    local fallback_file="$ss_home/fallback_events.jsonl"
    if [ -z "$fallback_record" ]; then
        return 0
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
            printf '%s\n' "$fallback_record" >> "$fallback_file"
        ) 9>"$lock_file" 2>/dev/null || {
            # flock or append failed — try plain >>
            printf '%s\n' "$fallback_record" >> "$fallback_file" 2>/dev/null || true
        }
    else
        # No flock — plain >> (PIPE_BUF interleaving risk for large payloads).
        printf '%s\n' "$fallback_record" >> "$fallback_file" 2>/dev/null || true
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

    local agent="${SECONDSIGHT_AGENT:-claude_code}"
    local port="${SECONDSIGHT_PORT:-8420}"
    local url="http://127.0.0.1:${port}/hook/${agent}/${event_type}"
    local fallback_record
    fallback_record="$(_secondsight_build_fallback_record "$event_type" "$payload_json")"
    if [ -z "$fallback_record" ]; then
        secondsight_fallback_append "$event_type" "$payload_json"
        return 0
    fi
    local post_body
    post_body="$(jq -c '{event_id,timestamp,sequence_number,payload}' <<<"$fallback_record" 2>/dev/null)"
    if [ -z "$post_body" ]; then
        _secondsight_fallback_append_record "$fallback_record" "$(_secondsight_resolve_home)"
        return 0
    fi

    # Check if curl is available before attempting.
    if ! command -v curl > /dev/null 2>&1; then
        printf 'secondsight_warning: curl not found; falling back to JSONL for event_type=%s.\n' \
            "$event_type" >&2
        _secondsight_fallback_append_record "$fallback_record" "$(_secondsight_resolve_home)"
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
            --data "$post_body" \
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
            _secondsight_fallback_append_record "$fallback_record" "$(_secondsight_resolve_home)"
            return 0
            ;;
    esac
}

_secondsight_extract_raw_payload() {
    local payload_json="$1"
    jq -c '
        if (type == "object" and has("event_id") and has("timestamp") and has("sequence_number") and has("payload"))
        then .payload
        else .
        end
    ' <<<"$payload_json" 2>/dev/null
}

_secondsight_extract_legacy_field() {
    local payload_json="$1"
    local field_name="$2"
    jq -r --arg field_name "$field_name" '
        if (type == "object" and has($field_name)) then .[$field_name] else empty end
    ' <<<"$payload_json" 2>/dev/null
}

_secondsight_lock_dir_for_session() {
    local ss_home="$1"
    local session_key="$2"
    local key_hash
    key_hash="$(printf '%s' "$session_key" | cksum | awk '{print $1}')"
    printf '%s/state/sequence/%s.lockdir' "$ss_home" "$key_hash"
}

_secondsight_counter_file_for_session() {
    local ss_home="$1"
    local session_key="$2"
    local key_hash
    key_hash="$(printf '%s' "$session_key" | cksum | awk '{print $1}')"
    printf '%s/state/sequence/%s.seq' "$ss_home" "$key_hash"
}

_secondsight_claim_sequence_number() {
    local session_key="$1"
    local ss_home="$2"
    local state_dir="$ss_home/state/sequence"
    mkdir -p "$state_dir" 2>/dev/null || return 1

    local lock_dir
    lock_dir="$(_secondsight_lock_dir_for_session "$ss_home" "$session_key")"
    local counter_file
    counter_file="$(_secondsight_counter_file_for_session "$ss_home" "$session_key")"

    while ! mkdir "$lock_dir" 2>/dev/null; do
        sleep 0.01
    done

    local current
    current="0"
    if [ -f "$counter_file" ]; then
        current="$(cat "$counter_file" 2>/dev/null || printf '0')"
    fi
    case "$current" in
        ''|*[!0-9]*)
            rmdir "$lock_dir" 2>/dev/null || true
            return 1
            ;;
    esac

    local next=$((current + 1))
    printf '%s' "$next" > "$counter_file" 2>/dev/null || {
        rmdir "$lock_dir" 2>/dev/null || true
        return 1
    }
    rmdir "$lock_dir" 2>/dev/null || true
    printf '%s' "$current"
    return 0
}

_secondsight_build_ingress_material() {
    local event_type="$1"
    local payload_json="$2"

    if ! command -v jq > /dev/null 2>&1; then
        printf 'secondsight_warning: jq not found; cannot parse session_id or generate ingress metadata for event_type=%s.\n' \
            "$event_type" >&2
        return 1
    fi

    local raw_payload
    raw_payload="$(_secondsight_extract_raw_payload "$payload_json")" || return 1
    [ -n "$raw_payload" ] || return 1

    local session_id
    session_id="$(jq -r '.session_id // empty' <<<"$raw_payload" 2>/dev/null)"
    if [ -z "$session_id" ]; then
        session_id="$(_secondsight_extract_legacy_field "$payload_json" "session_id")"
    fi
    if [ -z "$session_id" ]; then
        printf 'secondsight_warning: session_id missing in raw payload for event_type=%s; refusing to fabricate ordering key.\n' \
            "$event_type" >&2
        return 1
    fi

    local agent="${SECONDSIGHT_AGENT:-claude_code}"
    local ss_home
    ss_home="$(_secondsight_resolve_home)"

    local event_id
    local timestamp
    local sequence_number
    event_id="$(_secondsight_extract_legacy_field "$payload_json" "event_id")"
    timestamp="$(_secondsight_extract_legacy_field "$payload_json" "timestamp")"
    sequence_number="$(_secondsight_extract_legacy_field "$payload_json" "sequence_number")"

    if [ -z "$timestamp" ]; then
        timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || printf 'unknown')"
    fi

    if [ -z "$sequence_number" ]; then
        sequence_number="$(_secondsight_claim_sequence_number "${agent}:${session_id}" "$ss_home")" || {
            printf 'secondsight_warning: failed to claim sequence_number for session_id=%s event_type=%s.\n' \
                "$session_id" "$event_type" >&2
            return 1
        }
    fi

    if [ -z "$event_id" ]; then
        local seed checksum
        seed="${agent}:${event_type}:${session_id}:${timestamp}:${sequence_number}:${raw_payload}"
        checksum="$(printf '%s' "$seed" | cksum | awk '{print $1}')"
        event_id="evt-${checksum}-${sequence_number}"
    fi

    printf '%s\n%s\n%s\n%s\n%s\n' \
        "$event_id" \
        "$timestamp" \
        "$sequence_number" \
        "$raw_payload" \
        "$session_id"
}

_secondsight_build_fallback_record() {
    local event_type="$1"
    local payload_json="$2"
    local material
    material="$(_secondsight_build_ingress_material "$event_type" "$payload_json")" || return 1
    local event_id timestamp sequence_number raw_payload
    event_id="$(printf '%s\n' "$material" | sed -n '1p')"
    timestamp="$(printf '%s\n' "$material" | sed -n '2p')"
    sequence_number="$(printf '%s\n' "$material" | sed -n '3p')"
    raw_payload="$(printf '%s\n' "$material" | sed -n '4p')"
    local agent="${SECONDSIGHT_AGENT:-claude_code}"

    jq -c -n \
        --arg agent "$agent" \
        --arg event_type "$event_type" \
        --arg event_id "$event_id" \
        --arg timestamp "$timestamp" \
        --arg version "$_SECONDSIGHT_VERSION" \
        --argjson sequence_number "$sequence_number" \
        --argjson payload "$raw_payload" \
        '{
            agent: $agent,
            event_type: $event_type,
            event_id: $event_id,
            timestamp: $timestamp,
            sequence_number: $sequence_number,
            payload: $payload,
            hook_script_version: $version
        }' 2>/dev/null
}
