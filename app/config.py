"""Configuration, in one place.

No host is written into the code. The upstream base URL and the port are read,
in order, from the environment, then a local `.env`, then the `.env.example`
that ships with the repository. That ordering matters in both directions: an
explicit `FX_UPSTREAM_BASE=... ./run.sh` must never be overridden by a file left
lying around, and a fresh clone with no `.env` must still come up on the
documented default.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# .env is local and untracked; .env.example is committed and holds the defaults.
ENV_FILES = (ROOT / ".env", ROOT / ".env.example")


def _from_env_files(name: str) -> str | None:
    for path in ENV_FILES:
        value = _read(path, name)
        if value is not None:
            return value
    return None


def _read(path: Path, name: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return None


def setting(name: str) -> str | None:
    return os.environ.get(name) or _from_env_files(name)


def upstream_base() -> str:
    value = setting("FX_UPSTREAM_BASE")
    if not value:
        # Guessing a host here is exactly the hardcoding this indirection
        # exists to avoid, so refuse to start instead.
        raise RuntimeError(
            "FX_UPSTREAM_BASE is not set and neither .env nor .env.example "
            "supplies it. Export it, or restore .env.example."
        )
    return value.rstrip("/")
