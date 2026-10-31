#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
BREW_BIN="$(command -v brew || true)"
if [[ -z "$BREW_BIN" && -x /opt/homebrew/bin/brew ]]; then BREW_BIN="/opt/homebrew/bin/brew"; fi
[[ -n "$BREW_BIN" ]] || { printf 'Homebrew is missing. Run scripts/bootstrap.sh first.\n' >&2; exit 1; }
BREW_PREFIX="$("$BREW_BIN" --prefix)"
NODE_PREFIX="$BREW_PREFIX/opt/node@22/bin"
[[ -x "$NODE_PREFIX/node" && -x "$NODE_PREFIX/npm" ]] || { printf 'Pinned Node.js 22 is missing. Run scripts/bootstrap.sh first.\n' >&2; exit 1; }
export PATH="$NODE_PREFIX:$BREW_PREFIX/bin:$PATH"
exec "$NODE_PREFIX/npm" --prefix "$APP_ROOT/frontend" run dev
