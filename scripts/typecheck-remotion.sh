#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_NODE_MODULES="$APP_ROOT/frontend/node_modules"
REMOTION_ROOT="${REMOTION_STUDIO:-}"

# Read only the REMOTION_STUDIO value from the project .env; do not source the file.
if [[ -z "$REMOTION_ROOT" && -f "$APP_ROOT/.env" ]]; then
  while IFS='=' read -r key value; do
    key="${key//[[:space:]]/}"
    if [[ "$key" == "REMOTION_STUDIO" ]]; then
      value="${value%$'\r'}"
      value="${value#\"}"; value="${value%\"}"
      value="${value#\'}"; value="${value%\'}"
      REMOTION_ROOT="$value"
      break
    fi
  done < "$APP_ROOT/.env"
fi
REMOTION_ROOT="${REMOTION_ROOT:-$HOME/AI-Video-Studio/remotion-studio}"
REMOTION_ROOT="${REMOTION_ROOT/#\~/$HOME}"
REMOTION_ROOT="$(cd "$REMOTION_ROOT" && pwd)"

if [[ ! -x "$FRONTEND_NODE_MODULES/.bin/tsc" ]]; then
  echo "Frontend TypeScript is not installed. Run npm install in frontend/." >&2
  exit 1
fi
if [[ ! -d "$REMOTION_ROOT/node_modules/remotion" ]]; then
  echo "Remotion package not found under $REMOTION_ROOT/node_modules. Check REMOTION_STUDIO." >&2
  exit 1
fi

CONFIG_FILE="$(mktemp "${TMPDIR:-/tmp}/ai-video-studio-remotion-tsconfig.XXXXXX.json")"
trap 'rm -f "$CONFIG_FILE"' EXIT
APP_ROOT="$APP_ROOT" REMOTION_ROOT="$REMOTION_ROOT" CONFIG_FILE="$CONFIG_FILE" node <<'NODE'
const fs = require('node:fs');
const path = require('node:path');
const app = process.env.APP_ROOT;
const remotion = path.join(process.env.REMOTION_ROOT, 'node_modules');
const frontendTypes = path.join(app, 'frontend', 'node_modules', '@types', 'react');
const config = {
  compilerOptions: {
    strict: true,
    noEmit: true,
    skipLibCheck: true,
    esModuleInterop: true,
    allowSyntheticDefaultImports: true,
    jsx: 'react-jsx',
    moduleResolution: 'bundler',
    module: 'ESNext',
    target: 'ES2022',
    baseUrl: remotion,
    paths: {
      react: [path.join(frontendTypes, 'index.d.ts')],
      'react/jsx-runtime': [path.join(frontendTypes, 'jsx-runtime.d.ts')],
    },
  },
  files: [path.join(app, 'remotion', 'index.ts'), path.join(app, 'remotion', 'StoryboardComposition.tsx')],
};
fs.writeFileSync(process.env.CONFIG_FILE, JSON.stringify(config, null, 2));
NODE
"$FRONTEND_NODE_MODULES/.bin/tsc" -p "$CONFIG_FILE"
