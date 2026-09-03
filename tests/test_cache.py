"""The cache decides whether a caller gets a fresh rate or a remembered one, so
its expiry and eviction rules are worth pinning down on their own."""

import pytest

from app.cache import TTLCache
from app.errors import ERROR_STATUS, FxError


@pytest.fixture
def clock(monkeypatch):
    """A hand-cranked clock, so TTL tests do not sleep."""
    now = [1000.0]
    monkeypatch.setattr("app.cache.time.monotonic", lambda: now[0])
    return now


def test_stores_and_returns_a_value():
    cache = TTLCache()
    cache.put(("EUR", "TRY", "2026-08-28"), ("rate", "2026-08-28"), ttl=None)
    assert cache.get(("EUR", "TRY", "2026-08-28")) == ("rate", "2026-08-28")


def test_missing_key_is_a_miss_not_an_error():
    assert TTLCache().get(("EUR", "TRY", "latest")) is None


def test_entry_expires_once_its_ttl_has_passed(clock):
    cache = TTLCache()
    cache.put("k", "v", ttl=300)

    clock[0] += 299
    assert cache.get("k") == "v"

    clock[0] += 2
    assert cache.get("k") is None
    assert len(cache) == 0, "an expired entry should not linger"


def test_ttl_none_never_expires(clock):
    cache = TTLCache()
    cache.put("k", "v", ttl=None)
    clock[0] += 10_000_000
    assert cache.get("k") == "v"


def test_evicts_the_least_recently_used_entry_when_full():
    cache = TTLCache(max_entries=2)
    cache.put("a", 1, ttl=None)
    cache.put("b", 2, ttl=None)
    cache.get("a")  # 'a' is now the most recently used, so 'b' should go first
    cache.put("c", 3, ttl=None)

    assert len(cache) == 2
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_a_dead_entry_is_swept_before_a_live_one_is_evicted(clock):
    """An expired entry that happens to sit at the recent end must not push out
    an entry that is still good."""
    cache = TTLCache(max_entries=2)
    cache.put("dies", "d", ttl=100)
    cache.put("lives", "l", ttl=None)
    cache.get("dies")  # now the most recently used, so LRU alone would keep it
    clock[0] += 200

    cache.put("new", "n", ttl=None)

    assert cache.get("lives") == "l"
    assert cache.get("dies") is None


def test_writing_an_existing_key_replaces_its_ttl(clock):
    cache = TTLCache()
    cache.put("k", "v", ttl=10)
    cache.put("k", "v2", ttl=None)
    clock[0] += 1000
    assert cache.get("k") == "v2"


def test_writing_an_existing_key_makes_it_recent():
    cache = TTLCache(max_entries=2)
    cache.put("a", 1, ttl=None)
    cache.put("b", 2, ttl=None)
    cache.put("a", 3, ttl=None)  # refreshes 'a', so 'b' is now the oldest
    cache.put("c", 4, ttl=None)
    assert cache.get("a") == 3
    assert cache.get("b") is None


def test_a_cache_that_cannot_hold_anything_is_a_bug():
    with pytest.raises(ValueError):
        TTLCache(max_entries=0)


DOCUMENTED_CODES = [
        ("invalid_amount", 400),
        ("invalid_currency", 400),
        ("invalid_date", 400),
        ("invalid_request", 400),
        ("unknown_parameter", 400),
        ("date_in_future", 400),
        ("same_currency", 400),
        ("unknown_endpoint", 404),
        ("method_not_allowed", 405),
        ("unknown_currency", 404),
        ("no_rate_for_date", 404),
        ("rate_unavailable", 404),
        ("upstream_unavailable", 502),
        ("upstream_error", 502),
        ("upstream_invalid_response", 502),
        ("upstream_timeout", 504),
        ("internal_error", 500),
]


@pytest.mark.parametrize("code, status", DOCUMENTED_CODES)
def test_each_error_code_keeps_the_status_the_contract_promises(code, status):
    assert FxError(code, "x").status_code == status


def test_the_status_table_matches_the_documented_one_exactly():
    """A new code that nobody added to the README's table, or a renamed one,
    should fail here rather than reach a caller."""
    documented = {code for code, _ in DOCUMENTED_CODES}
    assert set(ERROR_STATUS) == documented


def test_an_unknown_error_code_is_a_bug_not_a_500():
    """Typos in a code must fail loudly at the raise site, not ship a response
    with a code no caller can look up."""
    with pytest.raises(KeyError):
        FxError("teh_upstream_bruk", "x")
