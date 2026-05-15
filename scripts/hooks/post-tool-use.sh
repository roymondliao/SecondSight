#!/usr/bin/env bash
# scripts/hooks/post-tool-use.sh — SecondSight PostToolUse hook.
#
# Install in ~/.claude/hooks/post-tool-use.sh (or symlink).
# Claude Code calls this after every tool invocation, passing a JSON payload
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
#   The script filename follows Claude Code's hook naming convention
#   (PostToolUse → post-tool-use.sh).  The URL posted to the SecondSight server
#   uses the canonical EventType enum value (tool_use_end), which uses
#   underscores, not hyphens.  These are intentionally different; do not change
#   the URL to match the filename.

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
# URL path uses canonical EventType enum value (tool_use_end), not the
# Claude Code hook filename prefix (post-tool-use).  See FILENAME vs URL NOTE above.
secondsight_post "tool_use_end" "$PAYLOAD"
exit 0
