"""What the client does with whatever the provider sends back.

The provider is the only source of a rate and of the day it belongs to, so most
of these tests are about refusing a payload rather than reading one.
"""

import asyncio
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.errors import FxError
from conftest import (
    BASE,
    CURRENCIES,
    FRIDAY,
    NOT_FOUND,
    SATURDAY,
    TODAY,
    make_upstream,
    rate_calls,
    rates_body,
    run,
)

FRIDAY_DATE = date.fromisoformat(FRIDAY)
SATURDAY_DATE = date.fromisoformat(SATURDAY)

GOOD = {
    "/v1/currencies": CURRENCIES,
    f"/v1/{FRIDAY}": (200, rates_body()),
    "/v1/latest": (200, rates_body(on=TODAY)),
}


def fetch(
    routes,
    *,
    on=FRIDAY_DATE,
    base="EUR",
    quote="TRY",
    today=TODAY,
    upstream_base=BASE,
    default=NOT_FOUND,
):
    upstream, calls = make_upstream(routes, default=default, base=upstream_base)
    return upstream, calls, lambda: run(upstream.get_rate(base, quote, on, today))


def refusal(routes, **kwargs):
    """Run a fetch that is expected to fail and return the FxError."""
    _, _, go = fetch(routes, **kwargs)
    with pytest.raises(FxError) as caught:
        go()
    return caught.value


# -- reading a good answer ------------------------------------------------


def test_returns_the_rate_and_the_day_it_belongs_to():
    _, _, go = fetch(GOOD)
    assert go() == (Decimal("56.1718"), FRIDAY)


def test_rate_keeps_every_digit_the_provider_sent():
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (200, rates_body(rate="56.171812345678901234"))}
    rate, _ = fetch(routes)[2]()
    assert rate == Decimal("56.171812345678901234")


def test_a_weekend_answer_keeps_the_providers_own_date():
    """Asked about a Saturday, the provider answers with Friday's rate and says
    so. That date is the one we must carry, not the one we asked for."""
    routes = dict(GOOD) | {f"/v1/{SATURDAY}": (200, rates_body(on=FRIDAY))}
    rate, rate_date = fetch(routes, on=SATURDAY_DATE)[2]()
    assert (rate, rate_date) == (Decimal("56.1718"), FRIDAY)


# -- refusing a bad answer ------------------------------------------------


@pytest.mark.parametrize(
    "payload, why",
    [
        (rates_body(base="USD"), "answered a different base currency"),
        (rates_body(amount="100"), "quoted per 100 units instead of per 1"),
        (rates_body(quote="JPY"), "returned a currency we did not ask for"),
        (rates_body(rate="0"), "a zero rate"),
        (rates_body(rate="-1.5"), "a negative rate"),
        (rates_body(rate="null"), "a null rate"),
        (rates_body(rate='"56.17"'), "a rate sent as a string"),
        (rates_body(rate="NaN"), "a NaN literal"),
        ('{"amount":1.0,"base":"EUR","rates":{"TRY":56.17}}', "no date at all"),
        ('{"amount":1.0,"base":"EUR","date":"not-a-date","rates":{"TRY":56.17}}', "an unparseable date"),
        ('{"amount":1.0,"base":"EUR","date":"2026-02-30","rates":{"TRY":56.17}}', "a date that is not on the calendar"),
        ("[]", "JSON that is not an object"),
        ("<html>502 Bad Gateway</html>", "a body that is not JSON"),
        ("", "an empty body"),
    ],
)
def test_refuses_a_payload_that_does_not_answer_the_question(payload, why):
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (200, payload)}
    assert refusal(routes).code == "upstream_invalid_response", why


def test_refuses_a_rate_dated_after_the_day_asked_about():
    """A later date is not a fallback -- it means the provider answered a
    different question, and 'the latest rate' must never be relabelled."""
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (200, rates_body(on=TODAY))}
    assert refusal(routes).code == "upstream_invalid_response"


# -- transport and status failures ----------------------------------------


@pytest.mark.parametrize(
    "failure, expected",
    [
        (httpx.ConnectError("refused"), "upstream_unavailable"),
        # A ConnectTimeout is a TimeoutException, but an upstream we cannot
        # reach is unavailable rather than slow -- this is the ordering trap.
        (httpx.ConnectTimeout("no route"), "upstream_unavailable"),
        (httpx.ReadTimeout("too slow"), "upstream_timeout"),
        (httpx.RemoteProtocolError("garbage"), "upstream_unavailable"),
    ],
)
def test_maps_transport_failures_to_the_right_code(failure, expected):
    assert refusal({}, default=failure).code == expected


@pytest.mark.parametrize("status", [500, 502, 429, 422])
def test_an_unexpected_status_is_never_turned_into_a_number(status):
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (status, '{"message":"nope"}')}
    assert refusal(routes).code == "upstream_error"


# -- telling an unknown currency from a day with no rate -------------------


def test_an_unknown_currency_is_rejected_without_asking_for_a_rate():
    upstream, calls, go = fetch(GOOD, quote="XYZ")
    with pytest.raises(FxError) as caught:
        go()
    assert caught.value.code == "unknown_currency"
    assert rate_calls(calls) == [], "the currency list already settled it"


def test_a_known_pair_with_no_rate_for_the_day_is_a_date_problem():
    routes = {"/v1/currencies": CURRENCIES}  # the dated path 404s
    assert refusal(routes).code == "no_rate_for_date"


def test_without_a_currency_list_the_reason_is_left_open():
    """If the provider has no /currencies endpoint we cannot tell the two
    reasons apart, so we must not assert either one."""
    error = refusal({})
    assert error.code == "rate_unavailable"
    assert "either" in error.message.lower()


# -- finding the upstream's path layout ------------------------------------


def test_works_against_an_upstream_that_serves_at_the_root():
    routes = {
        "/currencies": CURRENCIES,
        f"/{FRIDAY}": (200, rates_body()),
    }
    assert fetch(routes)[2]() == (Decimal("56.1718"), FRIDAY)


def test_a_base_that_already_carries_v1_is_not_doubled():
    routes = {"/v1/currencies": CURRENCIES, f"/v1/{FRIDAY}": (200, rates_body())}
    _, calls, go = fetch(routes, upstream_base="http://fake-upstream.test/v1")
    go()
    assert all("/v1/v1/" not in str(c.url) for c in calls)


def test_a_trailing_slash_on_the_base_does_not_produce_a_double_slash():
    _, calls, go = fetch(GOOD, upstream_base="http://fake-upstream.test/")
    go()
    assert all("//v1" not in str(c.url).removeprefix("http://") for c in calls)


# -- not re-asking the same question ---------------------------------------


def test_the_same_question_is_only_asked_once():
    upstream, calls, go = fetch(GOOD)
    assert go() == go()
    assert len(rate_calls(calls)) == 1


def test_a_different_date_is_a_different_question():
    """The bug this guards against reuses one cache entry for every date and
    labels it with whatever date the caller asked for."""
    routes = dict(GOOD) | {"/v1/2026-08-27": (200, rates_body(on="2026-08-27", rate="55.0"))}
    upstream, calls, go = fetch(routes)
    go()
    other = run(upstream.get_rate("EUR", "TRY", date(2026, 8, 27), TODAY))
    assert other == (Decimal("55.0"), "2026-08-27")
    assert len(rate_calls(calls)) == 2


def test_a_fallback_answer_is_also_filed_under_the_day_it_belongs_to():
    routes = dict(GOOD) | {f"/v1/{SATURDAY}": (200, rates_body(on=FRIDAY))}
    upstream, calls, _ = fetch(routes)
    run(upstream.get_rate("EUR", "TRY", SATURDAY_DATE, TODAY))
    run(upstream.get_rate("EUR", "TRY", FRIDAY_DATE, TODAY))
    assert len(rate_calls(calls)) == 1


def test_a_rate_that_could_still_change_is_not_kept_for_ever(monkeypatch):
    """Asked about today before the ECB has published, the provider answers
    with yesterday's rate. Keeping that for ever would serve a stale number
    long after the real one appeared."""
    clock = [1000.0]
    monkeypatch.setattr("app.cache.time.monotonic", lambda: clock[0])
    routes = dict(GOOD) | {f"/v1/{TODAY}": (200, rates_body(on=FRIDAY))}
    upstream, calls, _ = fetch(routes)

    run(upstream.get_rate("EUR", "TRY", date.fromisoformat(TODAY), TODAY))
    clock[0] += 301
    run(upstream.get_rate("EUR", "TRY", date.fromisoformat(TODAY), TODAY))
    assert len(rate_calls(calls)) == 2


def test_a_settled_past_rate_is_kept(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.cache.time.monotonic", lambda: clock[0])
    upstream, calls, go = fetch(GOOD)

    go()
    clock[0] += 100_000
    go()
    assert len(rate_calls(calls)) == 1


# -- believing only what is worth believing --------------------------------


def test_a_200_that_is_not_a_currency_list_is_not_believed():
    """Gateways and CDNs answer with 200-wrapped error bodies. Taking one for
    the currency list would have us telling a customer that EUR does not
    exist -- confidently, and for as long as the process lives."""
    routes = dict(GOOD) | {"/v1/currencies": (200, '{"message":"Not Found"}')}
    assert fetch(routes)[2]() == (Decimal("56.1718"), FRIDAY)


def test_without_a_believable_currency_list_the_reason_stays_open():
    routes = {"/v1/currencies": (200, '{"message":"Not Found"}')}
    assert refusal(routes, quote="XYZ").code == "rate_unavailable"


@pytest.mark.parametrize("rate", ["1e-400", "1e-13", "1e13", "1e400"])
def test_a_rate_outside_any_plausible_range_is_refused(rate):
    """1e-400 survives an is_finite() check and then rounds to 0.00 on the way
    out -- a silent zero wearing a 200."""
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (200, rates_body(rate=rate))}
    assert refusal(routes).code == "upstream_invalid_response"


def test_a_fallback_from_years_ago_is_refused_rather_than_served():
    """A frozen mirror answers honestly -- it says the rate is from 2019. The
    date is not the problem; presenting it as an answer for today is."""
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (200, rates_body(on="2019-01-02"))}
    error = refusal(routes)
    assert error.code == "rate_unavailable"
    assert "2019-01-02" in error.message


def test_a_holiday_length_gap_is_still_answered():
    routes = dict(GOOD) | {f"/v1/{FRIDAY}": (200, rates_body(on="2026-08-24"))}
    assert fetch(routes)[2]() == (Decimal("56.1718"), "2026-08-24")


def test_a_server_error_does_not_lock_in_the_wrong_prefix():
    """A 404 may be a real answer and a 500 proves nothing; neither should
    settle which layout this upstream serves."""
    routes = {f"/v1/{FRIDAY}": (500, "{}"), f"/{FRIDAY}": (200, rates_body())}
    upstream, _, go = fetch(routes)
    with pytest.raises(FxError) as caught:
        go()
    assert caught.value.code == "upstream_error"

    routes.pop(f"/v1/{FRIDAY}")  # the /v1 path stops erroring and starts 404ing
    assert go() == (Decimal("56.1718"), FRIDAY)


def test_two_requests_arriving_together_are_given_the_same_reason():
    """The discovery probe runs once. If a second caller slips past it while it
    is still in flight, the two get different explanations for the same
    question -- and one of them is a guess."""
    upstream, _ = make_upstream(GOOD)

    async def both():
        return await asyncio.gather(
            upstream.get_rate("EUR", "XYZ", FRIDAY_DATE, TODAY),
            upstream.get_rate("EUR", "XYZ", FRIDAY_DATE, TODAY),
            return_exceptions=True,
        )

    first, second = run(both())
    assert isinstance(first, FxError) and isinstance(second, FxError)
    assert first.code == second.code == "unknown_currency"
