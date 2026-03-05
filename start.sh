#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-1557}"

cleanup() {
  if [[ -n "${APP_PID:-}" ]] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" || true
  fi
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

echo "[start] app -> http://${HOST}:${PORT}"
PYTHONPATH=src python3 -c "from ariadne.api.http_server import run; run(host='${HOST}', port=${PORT})" &
APP_PID=$!

echo "[ready] frontend: http://${HOST}:${PORT}/"
echo "[ready] backend : http://${HOST}:${PORT}/api/v1/health/live"
echo "按 Ctrl+C 停止服务"

wait "$APP_PID"
