"""A small TTL + LRU cache, so a repeated question does not re-ask the upstream.

It knows nothing about currencies, dates or HTTP -- keys are tuples and values
are whatever the caller stores. Deliberately synchronous: there is no `await`
between reading and writing an entry, so two concurrent requests cannot
interleave inside it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Hashable

MAX_ENTRIES = 512


class TTLCache:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max = max_entries
        # key -> (expires_at | None, value); ordered oldest-used first
        self._data: OrderedDict[Hashable, tuple[float | None, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: Hashable, value: Any, ttl: float | None) -> None:
        """Store `value`. `ttl=None` means it never expires, which is only ever
        correct for a value that can never change."""
        expires_at = None if ttl is None else time.monotonic() + ttl
        self._data[key] = (expires_at, value)
        self._data.move_to_end(key)
        if len(self._data) > self._max:
            # Entries expire lazily, on read. Without this sweep a stale entry
            # that happens to sit near the recent end would push out a live one.
            self._drop_expired()
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def _drop_expired(self) -> None:
        now = time.monotonic()
        for key in [
            key
            for key, (expires_at, _) in self._data.items()
            if expires_at is not None and now >= expires_at
        ]:
            del self._data[key]

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
