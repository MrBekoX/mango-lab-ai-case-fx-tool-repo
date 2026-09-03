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

`FX_UPSTREAM_BASE` defaults to `https://api.frankfurter.dev`; no host is
hardcoded anywhere else. **One thing to know:** the real API serves under `/v1`,
which the documented default does not include. Rather than assume, the service
tries `<base>/v1/…` first, falls back to `<base>/…` on a 404, and remembers
whichever answered — so a fake upstream can serve at either.

## Test

```bash
./test.sh
```

105 tests, no network at all: every upstream response comes from an
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
  "rate_is_from_earlier_date": false,
  "source": "ECB via frankfurter.dev"
}
```

- **`rate_date`** — the day the rate actually belongs to, read from the
  provider's own `date` field. Never derived, never assumed.
- **`asked_date`** — the day you asked about. With no `date`, that is today.
- **`rate_is_from_earlier_date`** — `true` when the two differ. A ninth field
  beyond the brief's example: the two dates already carry the information, but a
  boolean is what a caller branches on without doing date arithmetic.

Failures return a status and:

```json
{ "error": "date_in_future", "message": "2030-01-01 is in the future; the ECB has not published a rate for it." }
```

## What it does in each case

| You ask about | It answers |
|---|---|
| A day the ECB published | `200`, `rate_date == asked_date` |
| **A weekend or holiday** | `200` with the last publication before it. `rate_date` is that earlier day and the flag is `true`, so the model can tell the customer which day the number is from |
| Nothing (`date` omitted) | `200` for the latest publication. `asked_date` is **today**, so asking on a Saturday still shows the rate is Friday's |
| A date in the future | `400 date_in_future`, refused without touching the upstream |
| A date before the series starts | `404 no_rate_for_date` |
| A currency code that does not exist | `404 unknown_currency` (or `400 invalid_currency` if it is not three letters) |
| The same currency twice | `400 same_currency` |
| An amount that is missing, zero, negative, `nan` or `inf` | `400 invalid_amount`, no upstream request |
| An amount with ten decimal places | `200`. Kept exact end to end as a `Decimal`; only `result` is rounded, to 2 places, half up |
| An upstream that is slow, down, returns 500, or returns something that is not JSON | `502` or `504`. Never a rate, never a zero |
| An upstream that answers a *different* question — wrong base currency, rates quoted per 100, a missing or later date, a rate of zero | `502 upstream_invalid_response` |

## Error codes

| Code | HTTP | Raised when |
|---|---|---|
| `invalid_amount` | 400 | `amount` missing, non-numeric, ≤ 0, non-finite, or above 1e12 |
| `invalid_currency` | 400 | `from`/`to` is not three letters |
| `invalid_date` | 400 | `date` is not a calendar date in `YYYY-MM-DD` |
| `unknown_parameter` | 400 | A query parameter this endpoint does not accept |
| `date_in_future` | 400 | `date` is after today (Europe/Berlin) |
| `same_currency` | 400 | `from` and `to` are the same |
| `unknown_currency` | 404 | The code is well formed but the ECB does not publish it |
| `no_rate_for_date` | 404 | Both codes are known, but no rate exists for that day |
| `rate_unavailable` | 404 | No rate, and the upstream offers no currency list to say which of the two reasons applies |
| `unknown_endpoint` / `method_not_allowed` | 404 / 405 | Wrong path or method |
| `upstream_unavailable` | 502 | Could not reach the provider |
| `upstream_error` | 502 | The provider returned an unexpected status |
| `upstream_invalid_response` | 502 | The provider's answer did not survive validation |
| `upstream_timeout` | 504 | The provider did not answer in time |
| `internal_error` | 500 | An unexpected fault; no rate is produced |

## Decisions worth knowing

- **`from == to` is an error, not `1.0`.** Answering would mean putting a rate in
  a 200 that no ECB publication stands behind, and giving it a date. The
  provider rejects the pair too. The message says the amount is unchanged, so
  the model can still answer the customer.
- **A repeated question does not re-ask the provider.** The cache key is
  `(from, to, date)` — not just the pair, so a question about 2015 can never be
  answered with today's rate. A rate for a day already over is kept
  indefinitely; anything else expires in five minutes, because today's rate is
  published mid-afternoon and a day we fell back from may still get its own.
- **`source` is a provenance label, not the address fetched from,** so it does
  not follow `FX_UPSTREAM_BASE`. It says where the numbers come from.

### Known limits

`result` is rounded to two decimals for every currency, which is wrong for JPY.
No retries. No single-flight, so "does not re-ask" holds for sequential calls;
two simultaneous identical requests will both go out. No auth or rate limiting —
none was asked for.
