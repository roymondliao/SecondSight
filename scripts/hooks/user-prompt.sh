#!/usr/bin/env bash
# scripts/hooks/user-prompt.sh — SecondSight UserPromptSubmit hook.
#
# Install in ~/.claude/hooks/user-prompt.sh (or symlink).
# Claude Code calls this when the user submits a prompt, passing a JSON payload
# on stdin.  This script ALWAYS exits 0.
#
# Environment variables (all optional):
#   SECONDSIGHT_PORT   — server port (default: 8420)
#   SECONDSIGHT_HOME   — home directory (default: ~/.secondsight)
#   SECONDSIGHT_AGENT  — agent identifier written into the fallback envelope
#
# hook_script_version: phase-2.0
#
# FILENAME vs URL NOTE (C1 fix):
#   The script filename uses hyphens (user-prompt.sh) per Claude Code convention.
#   The URL uses the canonical EventType enum value (user_prompt) with underscores.
#   These are intentionally different; do not change the URL to match the filename.

# NOT set -e: this hook must never exit non-zero.
set -u

# Resolve the real directory of THIS script even if invoked via symlink (C3 fix).
# `dirname "$0"` gives the directory of the *symlink*, not the real file.
# We follow the symlink chain via BASH_SOURCE and readlink to find the real
# script location, then source _lib.sh from there.
_SS_SOURCE="${BASH_SOURCE[0]}"
while [ -L "$_SS_SOURCE" ]; do
    _SS_DIR="$( cd -P "$( dirname "$_SS_SOURCE" )" && pwd )"
    _SS_SOURCE="$(readlink "$_SS_SOURCE")"
    # Handle relative symlinks: if the target is not absolute, prepend the
    # symlink's real directory to make it absolute.
    case "$_SS_SOURCE" in
        /*) : ;;  # already absolute
        *)  _SS_SOURCE="$_SS_DIR/$_SS_SOURCE" ;;
    esac
done
_SS_DIR="$( cd -P "$( dirname "$_SS_SOURCE" )" && pwd )"

# shellcheck source=_lib.sh
. "$_SS_DIR/_lib.sh"
secondsight_exit_if_disabled

PAYLOAD="$(cat)"

_ss_injection_log() {
    local message="$1"
    local ss_home
    ss_home="$(_secondsight_resolve_home)"
    local logs_dir="$ss_home/logs"
    mkdir -p "$logs_dir" 2>/dev/null || return 0
    printf 'secondsight_warning: user-prompt injection skipped: %s\n' "$message" \
        >> "$logs_dir/curl-errors.log" 2>/dev/null || true
    return 0
}

_ss_inject_prompt_guidance() {
    local payload_json="$1"
    command -v jq   > /dev/null 2>&1 || {
        _ss_injection_log "jq not found; cannot read prompt"
        return 0
    }
    command -v curl > /dev/null 2>&1 || {
        _ss_injection_log "curl not found; cannot call injection endpoint"
        return 0
    }

    local cwd
    cwd="$(printf '%s' "$payload_json" | jq -r '.cwd // empty' 2>/dev/null)"
    if [ -z "$cwd" ]; then
        _ss_injection_log "missing cwd"
        return 0
    fi

    local prompt
    prompt="$(printf '%s' "$payload_json" | jq -r '.prompt // empty' 2>/dev/null)"
    if [ -z "$prompt" ]; then
        _ss_injection_log "missing prompt"
        return 0
    fi

    local session_id
    session_id="$(printf '%s' "$payload_json" | jq -r '.session_id // empty' 2>/dev/null)"

    local body
    if [ -n "$session_id" ]; then
        body="$(jq -cn \
            --arg prompt "$prompt" \
            --arg cwd "$cwd" \
            --arg sid "$session_id" \
            '{"prompt": $prompt, "session_id": $sid, "cwd": $cwd}' 2>/dev/null)"
    else
        body="$(jq -cn \
            --arg prompt "$prompt" \
            --arg cwd "$cwd" \
            '{"prompt": $prompt, "session_id": null, "cwd": $cwd}' 2>/dev/null)"
    fi
    [ -n "$body" ] || return 0

    local port="${SECONDSIGHT_PORT:-8420}"
    local agent="${SECONDSIGHT_AGENT:-claude_code}"
    local ss_home
    ss_home="$(_secondsight_resolve_home)"
    local logs_dir="$ss_home/logs"
    mkdir -p "$logs_dir" 2>/dev/null || true
    local curl_error_log="$logs_dir/curl-errors.log"

    local injection_payload
    injection_payload="$(curl \
        --silent \
        --show-error \
        --fail \
        --connect-timeout 0.1 \
        --max-time 5.0 \
        --request POST \
        --header 'Content-Type: application/json' \
        --data-raw "$body" \
        "http://127.0.0.1:${port}/hook/injection/user-prompt/${agent}" \
        2>>"$curl_error_log")" || return 0

    [ -n "$injection_payload" ] && printf '%s' "$injection_payload"
    return 0
}

_ss_inject_prompt_guidance "$PAYLOAD"
secondsight_post "user_prompt" "$PAYLOAD"
exit 0
