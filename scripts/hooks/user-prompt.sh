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

_ss_resolve_cmd_path() {
    local cmd_name="$1"
    local cmd_path
    cmd_path="$(command -v "$cmd_name" 2>/dev/null)" || return 1
    if [ -L "$cmd_path" ]; then
        local target
        target="$(readlink "$cmd_path" 2>/dev/null)" || target=""
        case "$target" in
            /*) cmd_path="$target" ;;
            *) cmd_path="$(cd -P "$(dirname "$cmd_path")" 2>/dev/null && pwd)/$target" ;;
        esac
    fi
    printf '%s\n' "$cmd_path"
}

_ss_runtime_python_from_file() {
    local runtime_file="$_SS_DIR/.secondsight-hook-runtime.sh"
    if [ ! -f "$runtime_file" ]; then
        return 1
    fi
    # shellcheck source=/dev/null
    . "$runtime_file" 2>/dev/null || return 1
    if [ -n "${SECONDSIGHT_HOOK_PYTHON:-}" ] && [ -x "${SECONDSIGHT_HOOK_PYTHON:-}" ]; then
        printf '%s\n' "$SECONDSIGHT_HOOK_PYTHON"
        return 0
    fi
    return 1
}

_ss_inject_prompt_guidance() {
    local payload_json="$1"

    # -------------------------------------------------------------------------
    # Agent-native hit injection via Python render_wrapper.
    #
    # Resolution order:
    # 1. Read config [feedback].hit_injection_enabled via Python.
    #    If disabled → return 0 (no stdout, no injection).
    # 2. Parse payload + extract prompt inside Python.
    # 3. Invoke render_wrapper(prompt) via Python → emit JSON envelope → return 0.
    # 4. Any Python failure → log to curl-errors.log → return 0 (fail-open).
    # -------------------------------------------------------------------------

    local ss_home
    ss_home="$(_secondsight_resolve_home)"
    local logs_dir="$ss_home/logs"
    mkdir -p "$logs_dir" 2>/dev/null || true
    local curl_error_log="$logs_dir/curl-errors.log"

    # Determine Python interpreter.  Prefer an already-importable python on PATH
    # (typically an activated project venv), then fall back to `uv run --project`
    # for shells where the venv is not activated.
    # The project root is two levels above _SS_DIR (scripts/hooks → scripts → root).
    local ss_project_root
    ss_project_root="$(cd "$_SS_DIR/../.." 2>/dev/null && pwd 2>/dev/null)" || ss_project_root=""
    local python_probe='import secondsight.feedback.hit_injection'
    local python_path_prefix=""
    if [ -n "$ss_project_root" ] && [ -d "$ss_project_root/src" ]; then
        python_path_prefix="$ss_project_root/src"
    fi

    # Interpreter detection rationale (I-3):
    #   - Prefer python3/python already on PATH when they can import the local
    #     `secondsight` package. This avoids unnecessary uv startup/cache work
    #     for the common case where the project venv is already active.
    #   - The pyproject.toml guard avoids `uv run` when the hook is copied
    #     (not symlinked) to a directory two levels below a different project;
    #     if pyproject.toml is absent at `_SS_DIR/../..`, uv is skipped.
    #   - If bare python exists but cannot import `secondsight`, fall through
    #     to uv. That keeps copied hooks working when global python lacks the
    #     project deps.
    #
    # `python_launcher` holds the executable name: "uv" when uv path is used
    # (NOT a Python interpreter), or the resolved absolute path to python3/python.
    # `python_argv` holds any sub-command arguments (e.g. "run --project ...
    # python3" for uv; empty for the bare Python path).
    # Invocation: "$python_launcher" "${python_argv[@]}" <script_file>
    local python_launcher=""
    local -a python_argv=()
    local pinned_python=""
    local resolved_python3=""
    local resolved_python=""
    pinned_python="$(_ss_runtime_python_from_file)" || pinned_python=""
    resolved_python3="$(_ss_resolve_cmd_path python3)" || resolved_python3=""
    resolved_python="$(_ss_resolve_cmd_path python)" || resolved_python=""
    if [ -n "$pinned_python" ] \
            && PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                "$pinned_python" -c "$python_probe" > /dev/null 2>&1; then
        python_launcher="$pinned_python"
    elif [ -n "$resolved_python3" ] \
            && PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                "$resolved_python3" -c "$python_probe" > /dev/null 2>&1; then
        python_launcher="$resolved_python3"
    elif [ -n "$resolved_python" ] \
            && PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                "$resolved_python" -c "$python_probe" > /dev/null 2>&1; then
        python_launcher="$resolved_python"
    elif [ -n "$ss_project_root" ] && [ -f "$ss_project_root/pyproject.toml" ] \
            && command -v uv > /dev/null 2>&1; then
        python_launcher="uv"
        python_argv=("run" "--project" "$ss_project_root" "python3")
    else
        printf 'secondsight_warning: hit_injection: python3/python not found; injection skipped\n' \
            >> "$curl_error_log" 2>/dev/null || true
        # No interpreter available; fail-open with no injection (the observation
        # ingest via secondsight_post runs after this function returns).
        return 0
    fi

    # Invoke the internal SecondSight CLI entrypoint.  Payload parsing, config
    # loading, bypass checks, and JSON emission live in Python; the shell only
    # resolves a launcher and captures stdout/stderr.
    local python_output
    local python_stderr_file
    python_stderr_file="$(mktemp 2>/dev/null)" || python_stderr_file=""

    if [ -n "$python_stderr_file" ]; then
        if [ ${#python_argv[@]} -gt 0 ]; then
            python_output="$(
                printf '%s' "$payload_json" | \
                PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                    "$python_launcher" "${python_argv[@]}" -m secondsight hook user-prompt \
                    2>"$python_stderr_file"
            )"
        else
            python_output="$(
                printf '%s' "$payload_json" | \
                PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                    "$python_launcher" -m secondsight hook user-prompt \
                    2>"$python_stderr_file"
            )"
        fi
        local python_exit=$?
        # Log stderr when:
        #   (a) Python exited non-zero (error path), OR
        #   (b) stderr contains our config diagnostic prefix (hit_injection_enabled:
        #       or "hit_injection config read error:") even on exit 0 — this is the
        #       C1 fix: operators must see invalid-value diagnostics in the log.
        # We do NOT log all non-empty stderr on exit 0 to avoid polluting the log
        # with uv deprecation warnings that appear on every successful run.
        if [ -s "$python_stderr_file" ]; then
            if [ $python_exit -ne 0 ] || grep -q "hit_injection" "$python_stderr_file" 2>/dev/null; then
                if [ $python_exit -ne 0 ]; then
                    printf 'secondsight_warning: hit_injection python error: ' \
                        >> "$curl_error_log" 2>/dev/null || true
                fi
                cat "$python_stderr_file" >> "$curl_error_log" 2>/dev/null || true
            fi
        fi
        rm -f "$python_stderr_file" 2>/dev/null || true
    else
        # Fallback: no temp stderr file available; append stderr directly to the log.
        if [ ${#python_argv[@]} -gt 0 ]; then
            python_output="$(
                printf '%s' "$payload_json" | \
                PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                    "$python_launcher" "${python_argv[@]}" -m secondsight hook user-prompt \
                    2>>"$curl_error_log"
            )"
        else
            python_output="$(
                printf '%s' "$payload_json" | \
                PYTHONPATH="$python_path_prefix${PYTHONPATH:+:$PYTHONPATH}" \
                    "$python_launcher" -m secondsight hook user-prompt \
                    2>>"$curl_error_log"
            )"
        fi
        local python_exit=$?
    fi

    if [ $python_exit -ne 0 ]; then
        # Python exited non-zero: error already logged above (if stderr_file available).
        return 0
    fi

    if [ -n "$python_output" ]; then
        printf '%s' "$python_output"
    fi
    return 0
}

_ss_inject_prompt_guidance "$PAYLOAD"
secondsight_post "user_prompt" "$PAYLOAD"
exit 0
