#!/usr/bin/env bash
# SecondSight uninstaller — removes the uv tool only.
# Does NOT touch ~/.claude/ — hook injection and settings.json were
# created by `secondsight init` with explicit user consent, so they
# must be removed with explicit user action, not by this script.

set -euo pipefail

log()  { printf '\033[1;36m[uninstall]\033[0m %s\n' "$*"; }
hint() { printf '\033[1;33m[uninstall:hint]\033[0m %s\n' "$*"; }

if command -v uv >/dev/null 2>&1; then
  if uv tool list 2>/dev/null | grep -q '^secondsight'; then
    log "Removing secondsight uv tool..."
    uv tool uninstall secondsight
  else
    log "secondsight is not installed as a uv tool — nothing to remove."
  fi
else
  hint "uv not found — skipping tool uninstall."
fi

cat <<EOF

🗑  secondsight CLI removed.

Manual cleanup (this script intentionally does NOT do these for you):

  • Claude Code hooks + settings injected by 'secondsight init':
      Inspect:  cat ~/.claude/settings.json
      Hooks:    ls ~/.claude/hooks/ | grep secondsight
      Remove only the secondsight-related entries you actually want gone.

  • SecondSight runtime state (logs, sqlite db, sockets):
      Default location:  ~/.secondsight/   (or whatever --home you used)
      rm -rf ~/.secondsight/   # only if you're sure
EOF
