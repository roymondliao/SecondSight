#!/usr/bin/env bash
# scripts/hooks/session-end.sh — SecondSight SessionEnd hook.
#
# Install in ~/.claude/hooks/session-end.sh (or symlink).
# Claude Code calls this when a session ends, passing a JSON payload on stdin.
# This script ALWAYS exits 0.
#
# Environment variables (all optional):
#   SECONDSIGHT_PORT   — server port (default: 8420)
#   SECONDSIGHT_HOME   — home directory (default: ~/.secondsight)
#   SECONDSIGHT_AGENT  — agent identifier written into the fallback envelope
#
# hook_script_version: phase-1.2
#
# FILENAME vs URL NOTE (C1 fix):
#   The script filename uses hyphens (session-end.sh) per Claude Code convention.
#   The URL uses the canonical EventType enum value (session_end) with underscores.
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

PAYLOAD="$(cat)"
secondsight_post "session_end" "$PAYLOAD"
exit 0
