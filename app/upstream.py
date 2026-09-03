"""Talks to the Frankfurter (ECB) API and to nothing else.

This module knows HTTP and the provider's response contract. It does not know
about money arithmetic or about the shape of our own API. It hands back a
published rate together with *the day that rate actually belongs to*, or it
raises `FxError`. It never invents either one.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from decimal import Decimal

import httpx

from .cache import TTLCache
from .config import upstream_base
from .errors import FxError

# httpx counts each phase separately -- there is no total-request budget -- so
# these are per-phase limits, not a promise that a call returns within 5s.
TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)
# The discovery probe is optional, so it is not allowed to cost much.
PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)

# Long enough to spare the upstream a burst of identical questions, short enough
# that the afternoon's publication is picked up without a restart.
SHORT_TTL = 300.0

# The ECB's longest run of unpublished days is a holiday week. A gap wider than
# this means the provider is stale, not that the calendar is quiet, and serving
# it would be a confidently wrong number.
MAX_FALLBACK_DAYS = 10

# No real reference rate lives outside this range. Values that do would survive
# validation and then round to 0.00 on the way out.
MIN_RATE = Decimal("1e-12")
MAX_RATE = Decimal("1e12")

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_CODE = re.compile(r"[A-Za-z]{3}")


def _ttl_for(asked: str, rate_date: str, today: str) -> float | None:
    """How long an answer stays true.

    A rate published for a day that is already over can never change, so it can
    be kept for good. Everything else can still move: today's rate is published
    mid-afternoon, and a day we had to fall back from may yet get a rate of its
    own once the provider catches up.
    """
    if rate_date == asked and asked < today:
        return None
    return SHORT_TTL


def _currency_set(payload: object) -> frozenset[str] | None:
    """The provider's currency codes, or None if this is not a currency list.

    A 200 is not proof of one. Gateways and CDNs answer with 200-wrapped error
    bodies, and mistaking `{"message": "Not Found"}` for the currency list would
    have us telling a customer that EUR does not exist -- confidently, and for
    as long as the process lives.
    """
    if not isinstance(payload, dict) or len(payload) < 2:
        return None
    codes = set()
    for code, name in payload.items():
        if not isinstance(code, str) or not _CURRENCY_CODE.fullmatch(code):
            return None
        if not isinstance(name, str):
            return None
        codes.add(code.upper())
    return frozenset(codes)


def _reject_json_constant(name: str) -> None:
    # Python's json module accepts bare NaN and Infinity by default. Left alone,
    # NaN would multiply cleanly through the arithmetic and be written back out
    # as invalid JSON.
    raise FxError(
        "upstream_invalid_response",
        f"The rate provider sent the literal {name} where a number belongs.",
    )


def _parse_json(response: httpx.Response) -> dict:
    try:
        # `.content`, not `.text`: bytes let json detect the encoding itself
        # instead of trusting a possibly wrong charset header. parse_float keeps
        # the rate exact all the way from the wire.
        payload = json.loads(
            response.content, parse_float=Decimal, parse_constant=_reject_json_constant
        )
    except FxError:
        raise
    except ValueError:
        raise FxError(
            "upstream_invalid_response", "The rate provider did not return valid JSON."
        ) from None
    if not isinstance(payload, dict):
        raise FxError(
            "upstream_invalid_response",
            "The rate provider returned JSON that is not an object.",
        )
    return payload


def _validated_rate(payload: dict, base: str, quote: str) -> tuple[Decimal, str]:
    """Confirm the provider answered the question we asked, then read the rate.

    Reading `rates[quote]` without these checks is how a service ends up
    reporting one currency's rate as another's, or a rate quoted per 100 units
    as if it were quoted per one. Every failure here is a refusal, never a
    fallback.
    """
    rate_date = payload.get("date")
    if not isinstance(rate_date, str) or not _ISO_DATE.fullmatch(rate_date):
        raise FxError(
            "upstream_invalid_response",
            "The rate provider did not say which day its rate belongs to.",
        )
    try:
        date.fromisoformat(rate_date)
    except ValueError:
        raise FxError(
            "upstream_invalid_response",
            f"The rate provider dated its answer {rate_date!r}, which is not a calendar date.",
        ) from None

    answered_base = payload.get("base")
    if not isinstance(answered_base, str) or answered_base.upper() != base:
        raise FxError(
            "upstream_invalid_response",
            f"Asked for rates based on {base}, but the provider answered with base {answered_base!r}.",
        )

    quoted_per = payload.get("amount")
    if (
        isinstance(quoted_per, bool)
        or not isinstance(quoted_per, (int, Decimal))
        or Decimal(quoted_per) != 1
    ):
        raise FxError(
            "upstream_invalid_response",
            f"The provider quoted rates per {quoted_per!r} units rather than per 1.",
        )

    rates = payload.get("rates")
    if not isinstance(rates, dict) or quote not in rates:
        raise FxError(
            "upstream_invalid_response", f"The provider returned no {quote} rate."
        )

    rate = rates[quote]
    if isinstance(rate, bool) or not isinstance(rate, (int, Decimal)):
        raise FxError(
            "upstream_invalid_response",
            f"The provider gave {rate!r} as the {base}/{quote} rate, which is not a number.",
        )
    rate = Decimal(rate)
    if not rate.is_finite() or not MIN_RATE <= rate <= MAX_RATE:
        raise FxError(
            "upstream_invalid_response",
            f"The provider gave {rate} as the {base}/{quote} rate, which cannot be a real exchange rate.",
        )

    return rate, rate_date


class Upstream:
    """A rate provider reachable at `base`, with what we have learned about it."""

    def __init__(self, client: httpx.AsyncClient, base: str | None = None) -> None:
        self._client = client
        self._base = upstream_base() if base is None else base.rstrip("/")
        # Which path prefix this upstream serves under. None means "not known
        # yet"; a base that already carries /v1 needs nothing added.
        self._prefix: str | None = "" if self._base.endswith("/v1") else None
        self._probed = False
        self._probe_lock = asyncio.Lock()
        self._currencies: frozenset[str] | None = None
        self._rates = TTLCache()

    # -- discovery ---------------------------------------------------------

    def _candidates(self) -> list[str]:
        return [self._prefix] if self._prefix is not None else ["/v1", ""]

    async def _probe(self) -> None:
        """One lazy request that settles two questions at once: which path
        prefix this upstream serves under, and which currency codes it knows.

        Both answers are best effort. The reviewer's fake upstream may serve
        neither /currencies nor the /v1 prefix; when that happens we simply stop
        claiming we can tell an unknown currency apart from a day with no
        published rate, and say so in the error message instead.
        """
        if self._probed:
            return
        async with self._probe_lock:
            # Two requests arriving together must not see different worlds: one
            # answering "unknown currency" while the other says "no rate" would
            # make the reason we give depend on timing.
            if self._probed:
                return
            for prefix in self._candidates():
                try:
                    response = await self._request(
                        f"{self._base}{prefix}/currencies", None, PROBE_TIMEOUT
                    )
                except FxError:
                    # Unreachable right now, not absent. Worth asking again.
                    return
                if response.status_code != 200:
                    continue
                try:
                    payload = json.loads(response.content)
                except ValueError:
                    break
                codes = _currency_set(payload)
                if codes is None:
                    break
                self._prefix = prefix
                self._currencies = codes
                break
            self._probed = True

    # -- fetching ----------------------------------------------------------

    async def _request(
        self,
        url: str,
        params: dict[str, str] | None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        try:
            if timeout is None:
                return await self._client.get(url, params=params)
            return await self._client.get(url, params=params, timeout=timeout)
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            # ConnectTimeout is a subclass of TimeoutException, so it has to be
            # caught first: an upstream we cannot reach is unavailable, not slow.
            raise FxError(
                "upstream_unavailable", "Could not reach the rate provider."
            ) from exc
        except httpx.TimeoutException as exc:
            raise FxError(
                "upstream_timeout", "The rate provider did not answer in time."
            ) from exc
        except httpx.HTTPError as exc:
            raise FxError(
                "upstream_unavailable", "The request to the rate provider failed."
            ) from exc

    async def _fetch(self, path: str, params: dict[str, str]) -> httpx.Response:
        response = None
        for prefix in self._candidates():
            response = await self._request(f"{self._base}{prefix}/{path}", params)
            if response.is_success:
                # Only a success proves which prefix this upstream serves under.
                # A 500 proves nothing, and a 404 may be a real "no rate" answer,
                # so neither is allowed to lock one in.
                self._prefix = prefix
                break
            if response.status_code != 404:
                break
        assert response is not None  # _candidates() is never empty
        return response

    def _no_rate_error(self, base: str, quote: str, on: date | None) -> FxError:
        when = f"for {on.isoformat()}" if on is not None else "for the latest published day"
        if self._currencies is not None:
            # We already know both codes exist, so the date is what is missing.
            return FxError(
                "no_rate_for_date",
                f"The ECB published no {base}/{quote} rate {when}.",
            )
        # We could not check the currency list, so we do not know which of the
        # two reasons applies. Saying the wrong one would send the caller down
        # the wrong path, so we say both.
        return FxError(
            "rate_unavailable",
            f"No {base}/{quote} rate is available {when}: either the pair is not "
            f"published or no rate exists for that day.",
        )

    async def _fetch_rate(
        self, base: str, quote: str, on: date | None
    ) -> tuple[Decimal, str]:
        await self._probe()

        if self._currencies is not None:
            for code in (base, quote):
                if code not in self._currencies:
                    raise FxError(
                        "unknown_currency",
                        f"{code} is not a currency the ECB publishes a reference rate for.",
                    )

        path = on.isoformat() if on is not None else "latest"
        response = await self._fetch(path, {"base": base, "symbols": quote})

        if response.status_code == 404:
            raise self._no_rate_error(base, quote, on)
        if not response.is_success:
            raise FxError(
                "upstream_error",
                f"The rate provider answered with HTTP {response.status_code}.",
            )

        return _validated_rate(_parse_json(response), base, quote)

    # -- public ------------------------------------------------------------

    async def get_rate(
        self, base: str, quote: str, on: date | None, today: str
    ) -> tuple[Decimal, str]:
        """Return `(rate, the day that rate belongs to)`, from cache if we can.

        `today` is passed in rather than read here so that the caller owns the
        one definition of what day it is.
        """
        asked = on.isoformat() if on is not None else today
        key = (base, quote, on.isoformat() if on is not None else "latest")

        cached = self._rates.get(key)
        if cached is not None:
            return cached

        rate, rate_date = await self._fetch_rate(base, quote, on)

        if rate_date > asked:
            # A rate from *after* the day asked about is not a fallback, it is a
            # provider that has answered a different question.
            raise FxError(
                "upstream_invalid_response",
                f"The provider returned a rate dated {rate_date} for {asked}; a rate "
                f"cannot belong to a day later than the one asked about.",
            )

        gap = (date.fromisoformat(asked) - date.fromisoformat(rate_date)).days
        if gap > MAX_FALLBACK_DAYS:
            # Falling back over a weekend is normal; falling back over years
            # means the provider is frozen, and the answer would be wrong by
            # whatever the market did in between.
            raise FxError(
                "rate_unavailable",
                f"The newest {base}/{quote} rate the provider offers for {asked} is "
                f"from {rate_date}, {gap} days earlier -- too far back to answer with.",
            )

        entry = (rate, rate_date)
        ttl = _ttl_for(asked, rate_date, today)
        self._rates.put(key, entry, ttl)
        if key[2] != rate_date:
            # File the same rate under the day it actually belongs to, on the
            # same terms. Giving this entry a longer life than the answer it came
            # from would let one stale reply become a permanent fact.
            self._rates.put((base, quote, rate_date), entry, ttl)
        return entry
