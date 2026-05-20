#!/usr/bin/env bash
# SecondSight installer — detect + build + install only.
# Does NOT mutate ~/.claude/, ~/.bashrc, or any system path beyond what
# `uv tool install` manages (default: ~/.local/bin/secondsight).
# Failure mode: exit 1 with no cleanup — re-running is idempotent.

set -euo pipefail

REQUIRED_UV_MIN="0.5.0"
REQUIRED_NODE_MIN="20.19.0"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[install:err]\033[0m %s\n' "$*" >&2; }
hint() { printf '\033[1;33m[install:hint]\033[0m %s\n' "$*" >&2; }

version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

check_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    err "uv not found."
    hint "Install uv (user-space, no sudo):"
    hint "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    hint "Then re-open your shell or:  source \$HOME/.local/bin/env"
    exit 1
  fi
  local have
  have="$(uv --version | awk '{print $2}')"
  if ! version_ge "$have" "$REQUIRED_UV_MIN"; then
    err "uv $have is too old (need >= $REQUIRED_UV_MIN)."
    hint "Upgrade:  uv self update"
    exit 1
  fi
  log "uv $have ✓"
}

check_node() {
  if ! command -v node >/dev/null 2>&1; then
    err "node not found."
    hint "Install Node $REQUIRED_NODE_MIN+ via one of:"
    hint "  nvm:  nvm install $REQUIRED_NODE_MIN && nvm use $REQUIRED_NODE_MIN"
    hint "  fnm:  fnm install $REQUIRED_NODE_MIN && fnm use $REQUIRED_NODE_MIN"
    hint "  or download from https://nodejs.org/"
    exit 1
  fi
  local have
  have="$(node --version | sed 's/^v//')"
  if ! version_ge "$have" "$REQUIRED_NODE_MIN"; then
    err "node $have is too old (need >= $REQUIRED_NODE_MIN for vite 7)."
    hint "Upgrade via your Node version manager and re-run."
    exit 1
  fi
  log "node $have ✓"
}

check_npm() {
  if ! command -v npm >/dev/null 2>&1; then
    err "npm not found (should come with Node)."
    exit 1
  fi
  log "npm $(npm --version) ✓"
}

build_frontend() {
  log "Building frontend dashboard (npm ci + vite build)..."
  (cd "$REPO_ROOT/frontend" && npm ci && npm run build)
  if [ ! -d "$REPO_ROOT/frontend/dist" ]; then
    err "frontend/dist not produced — wheel would ship without dashboard."
    exit 1
  fi
  log "frontend/dist ready ✓"
}

install_python_pkg() {
  log "Installing secondsight as a uv tool..."
  (cd "$REPO_ROOT" && uv tool install --force --reinstall .)
}

verify_cli() {
  if ! command -v secondsight >/dev/null 2>&1; then
    err "secondsight installed but not in PATH."
    hint "uv tool installs to ~/.local/bin by default. Add to your shell rc:"
    hint "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    hint "Then re-open your shell and run:  secondsight --version"
    exit 1
  fi
  log "$(secondsight --version) ✓"
}

main() {
  log "SecondSight install — repo: $REPO_ROOT"
  check_uv
  check_node
  check_npm
  build_frontend
  install_python_pkg
  verify_cli
  cat <<EOF

✅ SecondSight installed successfully.

Next steps (these will mutate ~/.claude/ — review before running):
  1. Inject Claude Code hooks + settings:
       secondsight init
  2. Start the dashboard daemon:
       secondsight serve --daemon
  3. Confirm it is running:
       secondsight status

To uninstall:
  $REPO_ROOT/uninstall.sh
EOF
}

main "$@"
