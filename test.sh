#!/usr/bin/env bash
# Runs the tests. They never reach the network: every upstream response comes
# from an httpx.MockTransport, so $FX_UPSTREAM_BASE can point anywhere, closed
# port included, and the suite still passes.
set -euo pipefail
cd "$(dirname "$0")"

# See run.sh -- the environment always wins over .env.
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

PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$VENV/Scripts/python.exe"

if ! "$PY" -c 'import fastapi, httpx, pytest' >/dev/null 2>&1; then
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt ||
    { echo "Could not install dependencies from requirements.txt" >&2; exit 1; }
fi

exec "$PY" -m pytest -q
