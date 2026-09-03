# fx-tool

One endpoint an agent can call to convert an amount between two currencies,
using the ECB reference rates published by [frankfurter.dev](https://frankfurter.dev).

Built around one rule: **a wrong number is worse than no number.** Every 200
carries a rate the ECB actually published, together with the day that rate
belongs to. Everything else is a refusal with a status and a code.

## Run

```bash
./run.sh                                        # http://localhost:8080
PORT=9000 ./run.sh
FX_UPSTREAM_BASE=http://localhost:9999 ./run.sh
```

Config order: **environment → `.env` → `.env.example`**. No provider host is
written in the Python; the committed `.env.example` carries the documented
default, `.env` is local and untracked, and the environment beats both.

**One thing to know:** the real API serves under `/v1`, which the documented
default base does not include. Rather than assume, the service tries
`<base>/v1/…`, falls back to `<base>/…`, and remembers whichever answered — so a
fake upstream can serve at either.

## Test

```bash
./test.sh
```

120 tests, no network at all: every upstream response comes from an
`httpx.MockTransport`. Confirmed green with `FX_UPSTREAM_BASE` pointed at a
closed port.

## The endpoint

```
GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28
```

`date` is optional — omit it for the latest published rate. Codes are
case-insensitive.

```json
{
  "amount": 250,
  "from": "EUR",
  "to": "TRY",
  "rate": 56.1718,
  "result": 14042.95,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "source": "ECB via frankfurter.dev"
}
```

- **`rate_date`** — the day the rate belongs to, read from the provider's own
  `date` field. Never derived.
- **`asked_date`** — the day you asked about; with no `date`, today. So asking
  "what is it now?" on a Saturday still shows the rate is Friday's.

When they differ, the rate is from an earlier publication. Failures return a
status and:

```json
{ "error": "date_in_future", "message": "2030-01-01 is in the future; the ECB has not published a rate for it." }
```

## What it does in each case

| You ask about | It answers |
|---|---|
| A day the ECB published | `200`, `rate_date == asked_date` |
| **A weekend or holiday** | `200` with the last publication before it, and `rate_date` is that earlier day |
| Nothing (`date` omitted) | `200` for the latest publication, with `asked_date` set to today |
| A date in the future | `400 date_in_future`, refused without touching the upstream |
| A date before the series starts | `404 no_rate_for_date` |
| A currency code that does not exist | `404 unknown_currency` (or `400 invalid_currency` if it is not three letters) |
| The same currency twice | `400 same_currency` |
| An amount that is missing, zero, negative, `nan`, `inf`, or written as `250_0` | `400 invalid_amount`, no upstream request |
| An amount with ten decimal places | `200`. Kept exact end to end as a `Decimal`; only `result` is rounded, to 2 places, half up |
| A `date` that is a bare number, or given twice | `400`. Pydantic would read `1756339200` as a Unix timestamp and answer about a different day |
| An upstream that is slow, down, returns 500, or returns something that is not JSON | `502` or `504`. Never a rate, never a zero |
| An upstream that answers a *different* question — wrong base currency, rates quoted per 100, a missing or later date, an implausible rate | `502 upstream_invalid_response` |
| An upstream whose newest rate is more than ten days older than the day asked about | `404 rate_unavailable`. A holiday gap is normal; a frozen mirror is not |

## Error codes

| Code | HTTP | Raised when |
|---|---|---|
| `invalid_amount` | 400 | `amount` missing, not a plain number, ≤ 0, non-finite, or above 1e12 |
| `invalid_currency` | 400 | `from`/`to` is not three letters |
| `invalid_date` | 400 | `date` is not a calendar date written as `YYYY-MM-DD` |
| `invalid_request` | 400 | A parameter was given more than once |
| `unknown_parameter` | 400 | A query parameter this endpoint does not accept |
| `date_in_future` | 400 | `date` is after today (Europe/Berlin) |
| `same_currency` | 400 | `from` and `to` are the same |
| `unknown_currency` | 404 | The code is well formed but the ECB does not publish it |
| `no_rate_for_date` | 404 | Both codes are known, but no rate exists for that day |
| `rate_unavailable` | 404 | No usable rate, and either the upstream offers no currency list to say why, or the newest one is too old to serve |
| `unknown_endpoint` / `method_not_allowed` | 404 / 405 | Wrong path or method |
| `upstream_unavailable` | 502 | Could not reach the provider |
| `upstream_error` | 502 | The provider returned an unexpected status |
| `upstream_invalid_response` | 502 | The provider's answer did not survive validation |
| `upstream_timeout` | 504 | The provider did not answer in time |
| `internal_error` | 500 | An unexpected fault; no rate is produced |

## Decisions worth knowing

- **`from == to` is an error, not `1.0`** — answering would put a rate in a 200
  that no ECB publication stands behind, and date it. The message says the
  amount is unchanged, so the model can still answer.
- **The provider's answer is checked before it is believed:** base currency
  matches, rates quoted per one unit, requested currency present, rate finite
  and plausible. Reading `rates[to]` without that is how a service reports one
  currency's rate as another's.
- **A repeat does not re-ask the provider.** The key is `(from, to, date)` — not
  just the pair — so a question about 2015 can never be answered with today's
  rate. A day already over is kept indefinitely; anything else for five minutes.
- **`source` is a provenance label,** not the address fetched from, so it does
  not follow `FX_UPSTREAM_BASE`.

### Known limits

`result` is rounded to two decimals for every currency, which is wrong for JPY.
No retries. No single-flight, so "does not re-ask" holds for sequential calls;
two simultaneous identical requests will both go out. The currency list is
fetched once and kept for the life of the process. `/tools/convert` is the only
endpoint written here; FastAPI's own `/docs` and `/openapi.json` are left on,
since the schema is how an agent discovers the tool. No auth or rate limiting —
none was asked for.
