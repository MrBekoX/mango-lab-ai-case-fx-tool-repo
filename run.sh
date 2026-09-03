#!/usr/bin/env bash
# Starts the service. It listens on $PORT (default 8080) and takes the upstream
# base URL from $FX_UPSTREAM_BASE (see app/upstream.py) -- no host is named here.
set -euo pipefail
cd "$(dirname "$0")"

# A local .env is a developer convenience; see .env.example. Anything already in
# the environment wins, so `FX_UPSTREAM_BASE=... ./run.sh` is never overridden by
# a stale file -- quietly talking to the wrong upstream is the one failure this
# service exists to prevent.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    key=${key%%[![:alnum:]_]*}
    [ -n "$key" ] || continue
    if [ -z "${!key-}" ]; then export "$key=$value"; fi
  done < <(tr -d '\r' < .env)
fi

VENV=.venv
if [ ! -d "$VENV" ]; then
  BOOTSTRAP="$(command -v python3 || command -v python)"
  "$BOOTSTRAP" -m venv "$VENV"
fi

# Virtualenvs keep the interpreter in bin/ on POSIX and Scripts/ on Windows.
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$VENV/Scripts/python.exe"

# Only reach for the network when something is actually missing, so a machine
# that already has the dependencies never needs it.
if ! "$PY" -c 'import fastapi, uvicorn, httpx' >/dev/null 2>&1; then
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt ||
    { echo "Could not install dependencies from requirements.txt" >&2; exit 1; }
fi

exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
