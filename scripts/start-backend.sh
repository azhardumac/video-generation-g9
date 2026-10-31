#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
API_HOST_OVERRIDE="${API_HOST+x}"
API_PORT_OVERRIDE="${API_PORT+x}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
if [[ -f "$APP_ROOT/.env" ]]; then
  while IFS='=' read -r key value; do
    key="${key//[[:space:]]/}"
    value="${value%$'\r'}"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
    [[ "$key" == "API_HOST" && -z "$API_HOST_OVERRIDE" ]] && API_HOST="$value"
    [[ "$key" == "API_PORT" && -z "$API_PORT_OVERRIDE" ]] && API_PORT="$value"
  done < "$APP_ROOT/.env"
fi
PYTHON_ENV="$APP_ROOT/backend/.venv/bin"
[[ -x "$PYTHON_ENV/uvicorn" ]] || { printf 'Backend environment missing. Run scripts/bootstrap.sh first.\n' >&2; exit 1; }
exec "$PYTHON_ENV/uvicorn" app.main:app --app-dir "$APP_ROOT/backend" --host "$API_HOST" --port "$API_PORT"
