"""What a caller actually receives from GET /tools/convert.

The endpoint is called by a language model that is talking to a paying
customer, so these tests care about two things above all: that a 200 never
carries a number the ECB did not publish, and that the day a rate belongs to is
never presented as a day it does not.
"""

import json

import httpx
import pytest

from conftest import CURRENCIES, FRIDAY, NOT_FOUND, SATURDAY, TODAY, rates_body

SHAPE = {
    "amount",
    "from",
    "to",
    "rate",
    "result",
    "rate_date",
    "asked_date",
    "source",
}

GOOD = {
    "/v1/currencies": CURRENCIES,
    f"/v1/{FRIDAY}": (200, rates_body()),
    "/v1/latest": (200, rates_body(on=TODAY)),
}

ASK = {"amount": "250", "from": "EUR", "to": "TRY", "date": FRIDAY}


def convert(client, **overrides):
    params = {**ASK, **overrides}
    return client.get(
        "/tools/convert", params={k: v for k, v in params.items() if v is not None}
    )


# -- the answer -----------------------------------------------------------


def test_a_business_day_answers_with_the_rate_for_that_day(client):
    api, _ = client(GOOD)
    response = convert(api)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SHAPE
    assert body["rate"] == 56.1718
    assert body["result"] == 14042.95  # 250 x 56.1718
    assert body["rate_date"] == body["asked_date"] == FRIDAY
    assert body["source"] == "ECB via frankfurter.dev"


def test_numbers_are_json_numbers_not_strings(client):
    """A model reading `"rate": "56.1718"` has to unquote it first, and the
    brief's example shows a bare number."""
    api, _ = client(GOOD | {f"/v1/{FRIDAY}": (200, rates_body(rate="56.1718"))})
    raw = json.loads(convert(api).text)
    assert isinstance(raw["rate"], float) and raw["rate"] == 56.1718
    assert isinstance(raw["result"], float)
    assert raw["amount"] == 250 and not isinstance(raw["amount"], str)


def test_a_weekend_says_which_day_the_rate_is_really_from(client):
    """The ECB published nothing on the Saturday. Answering with Friday's rate
    is fine; presenting it as Saturday's is not."""
    api, _ = client(GOOD | {f"/v1/{SATURDAY}": (200, rates_body(on=FRIDAY))})
    body = convert(api, date=SATURDAY).json()

    assert body["asked_date"] == SATURDAY
    assert body["rate_date"] == FRIDAY
    assert body["rate_date"] < body["asked_date"]


def test_asking_without_a_date_still_reveals_a_stale_rate(client):
    """Today is a Saturday and the newest publication is Friday's. The caller
    asked about "now", so `asked_date` is today -- otherwise the two dates would
    always match and the difference would be invisible on the most common call
    there is."""
    api, _ = client(GOOD | {"/v1/latest": (200, rates_body(on=FRIDAY))}, now=SATURDAY)
    body = convert(api, date=None).json()

    assert body["asked_date"] == SATURDAY
    assert body["rate_date"] == FRIDAY


def test_asking_without_a_date_on_a_published_day_matches(client):
    api, _ = client(GOOD)
    body = convert(api, date=None).json()
    assert body["rate_date"] == body["asked_date"] == TODAY


def test_ten_decimal_places_survive_the_arithmetic(client):
    api, _ = client(GOOD | {f"/v1/{FRIDAY}": (200, rates_body(rate="2.0"))})
    body = convert(api, amount="0.1234567891").json()
    assert body["result"] == 0.25  # 0.2469135782 rounded half-up
    assert body["rate"] == 2.0


def test_lowercase_currency_codes_are_accepted(client):
    api, _ = client(GOOD)
    body = convert(api, **{"from": "eur", "to": "try"}).json()
    assert body["from"] == "EUR" and body["to"] == "TRY"


# -- refusals that never reach the provider --------------------------------


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"date": "2099-01-01"}, "date_in_future"),
        ({"to": "EUR"}, "same_currency"),
        ({"from": "eur", "to": "EUR"}, "same_currency"),
        ({"from": "EURO"}, "invalid_currency"),
        ({"from": "12A"}, "invalid_currency"),
        ({"from": None}, "invalid_currency"),
        ({"amount": None}, "invalid_amount"),
        ({"amount": "0"}, "invalid_amount"),
        ({"amount": "-5"}, "invalid_amount"),
        ({"amount": "nan"}, "invalid_amount"),
        ({"amount": "inf"}, "invalid_amount"),
        ({"amount": "abc"}, "invalid_amount"),
        ({"amount": "1e13"}, "invalid_amount"),
        # Decimal would read this as 2500 -- ten times what was written.
        ({"amount": "250_0"}, "invalid_amount"),
        ({"amount": "1_000"}, "invalid_amount"),
        ({"date": "2026-13-45"}, "invalid_date"),
        ({"date": "yesterday"}, "invalid_date"),
        ({"date": ""}, "invalid_date"),
        # Pydantic would read a bare number as a Unix timestamp and answer
        # about an entirely different day.
        ({"date": "1756339200"}, "invalid_date"),
        ({"date": "0"}, "invalid_date"),
    ],
)
def test_a_bad_request_is_refused_without_asking_the_provider(client, overrides, code):
    api, calls = client(GOOD)
    response = convert(api, **overrides)
    assert response.status_code == 400
    assert response.json()["error"] == code
    assert calls == [], "no request should reach the provider"


def test_an_unrecognised_parameter_is_refused_rather_than_ignored(client):
    """`on=` was the old parameter name. Ignoring it would quietly answer about
    today while the caller believes it asked about August."""
    api, calls = client(GOOD)
    response = api.get(
        "/tools/convert",
        params={"amount": "250", "from": "EUR", "to": "TRY", "on": FRIDAY},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unknown_parameter"
    assert "on" in response.json()["message"]
    assert calls == []


def test_a_parameter_given_twice_is_refused(client):
    """Only one of the two values would be used, and the caller would have no
    way to know which."""
    api, calls = client(GOOD)
    response = api.get(
        f"/tools/convert?amount=250&from=EUR&to=TRY&date={FRIDAY}&date=2026-08-27"
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert calls == []


# -- refusals that come back from the provider -----------------------------


@pytest.mark.parametrize(
    "routes, default, status, code",
    [
        (
            {**GOOD, f"/v1/{FRIDAY}": httpx.ConnectError("refused")},
            NOT_FOUND,
            502,
            "upstream_unavailable",
        ),
        (
            {**GOOD, f"/v1/{FRIDAY}": httpx.ConnectTimeout("no route")},
            NOT_FOUND,
            502,
            "upstream_unavailable",
        ),
        (
            {**GOOD, f"/v1/{FRIDAY}": httpx.ReadTimeout("slow")},
            NOT_FOUND,
            504,
            "upstream_timeout",
        ),
        ({**GOOD, f"/v1/{FRIDAY}": (500, "{}")}, NOT_FOUND, 502, "upstream_error"),
        (
            {**GOOD, f"/v1/{FRIDAY}": (200, "<html>oops</html>")},
            NOT_FOUND,
            502,
            "upstream_invalid_response",
        ),
        (
            {**GOOD, f"/v1/{FRIDAY}": (200, rates_body(base="USD"))},
            NOT_FOUND,
            502,
            "upstream_invalid_response",
        ),
        (
            {**GOOD, f"/v1/{FRIDAY}": (200, rates_body(rate="0"))},
            NOT_FOUND,
            502,
            "upstream_invalid_response",
        ),
        ({"/v1/currencies": CURRENCIES}, NOT_FOUND, 404, "no_rate_for_date"),
        ({}, NOT_FOUND, 404, "rate_unavailable"),
    ],
)
def test_a_provider_problem_never_becomes_a_number(
    client, routes, default, status, code
):
    api, _ = client(routes, default=default)
    response = convert(api)
    assert response.status_code == status
    assert response.json() == {"error": code, "message": response.json()["message"]}


def test_an_unknown_currency_is_named_as_such(client):
    api, _ = client(GOOD)
    response = convert(api, to="XYZ")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_currency"


def test_a_rate_from_years_ago_is_refused_rather_than_served(client):
    """A frozen mirror still answers, and its answer is still honestly dated.
    Serving it anyway would be wrong by whatever the market did in between."""
    api, _ = client(GOOD | {f"/v1/{FRIDAY}": (200, rates_body(on="2019-01-02"))})
    response = convert(api)
    assert response.status_code == 404
    assert response.json()["error"] == "rate_unavailable"


# -- the error envelope ----------------------------------------------------


@pytest.mark.parametrize(
    "path, method, code",
    [
        ("/tools/covert", "get", "unknown_endpoint"),
        ("/tools/convert", "post", "method_not_allowed"),
    ],
)
def test_routing_mistakes_use_our_error_shape_not_the_frameworks(
    client, path, method, code
):
    """FastAPI's own body is {"detail": ...}. A caller that only knows our
    contract would not find an error code in it."""
    api, _ = client(GOOD)
    response = getattr(api, method)(path)
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == code


def test_an_unexpected_crash_still_answers_in_the_error_shape(client, monkeypatch):
    api, _ = client(GOOD)
    monkeypatch.setattr(
        "app.main.today", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    response = convert(api)
    assert response.status_code == 500
    assert set(response.json()) == {"error", "message"}
    assert response.json()["error"] == "internal_error"


def test_every_failure_body_has_exactly_two_fields(client):
    api, _ = client(GOOD)
    for overrides in (
        {"amount": "0"},
        {"from": "EURO"},
        {"date": "2099-01-01"},
        {"to": "EUR"},
    ):
        body = convert(api, **overrides).json()
        assert set(body) == {"error", "message"}
        assert body["message"].endswith(".") and " " in body["message"]


# -- not re-asking ---------------------------------------------------------


def test_repeating_the_same_question_does_not_reask_the_provider(client):
    api, calls = client(GOOD)
    first, second = convert(api).json(), convert(api).json()
    assert first == second
    assert len([c for c in calls if not c.url.path.endswith("/currencies")]) == 1
