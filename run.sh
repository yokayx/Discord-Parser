#!/usr/bin/env bash
# Запуск (Linux/macOS): ./run.sh
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
echo "Discord Parser: http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
