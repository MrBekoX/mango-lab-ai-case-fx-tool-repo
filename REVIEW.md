# Review of tool.py

It runs, and that is the problem: almost everything that goes wrong here comes
back as `200 OK` with a number in it. Ranked by what reaches a customer.

## 1. The cache ignores the date, so one answer is sold as every date — lines 21, 28-30

`key = f"{base}-{target}"`. The date is in neither the key nor the value, there
is no TTL, and on a hit the answer is labelled with **whatever date the caller
asked for**.

```
GET ...&on=2026-01-02  → asks upstream, rate 47.12, rate_date 2026-01-02
GET ...                → no upstream call. rate 47.12, rate_date "today"
GET ...&on=2020-06-15  → no upstream call. rate 47.12, rate_date "2020-06-15"
```

EUR/TRY was ~7.7 in June 2020, so that last answer is out by **510%**, and
nothing in it hints at that — `rate_date` says exactly what was asked. One
user's historical lookup poisons every later user's "current" rate for the life
of the process.

**Verify:** run those three calls and watch `rate` never change;
`python -c "import tool; print(tool._cache)"` shows one key, no date.

## 2. The rate is rounded to two decimals *before* the multiplication — lines 60-61

Not a display detail: line 61 multiplies by the damaged value. Real ECB rates,
for 1,000,000 units:

| Pair | Real rate | Used | Correct | Told to the customer |
|---|---|---|---|---|
| HUF→EUR | 0.00272 | **0.0** | 2,720.00 | **0.00** |
| KRW→EUR | 0.00063 | **0.0** | 630.00 | **0.00** |
| JPY→EUR | 0.00552 | 0.01 | 5,520.00 | **10,000.00** (+81%) |
| TRY→EUR | 0.01782 | 0.02 | 17,820.00 | **20,000.00** (+12.2%) |

Any base whose rate is under 0.005 converts to exactly **zero** — and that zero
is byte-identical to the error response in finding 3, so afterwards nobody can
tell the two apart. This needs no special input; it damages every successful
response.

**Verify:** `curl ".../tools/convert?amount=1000000&from_=JPY&to=EUR"` → `rate
0.01, result 10000.0`.

## 3. Every failure becomes `200 OK` with `rate: 0.0` — lines 71-81

The bare `except Exception` swallows everything and returns a body
indistinguishable from success, `source: "ECB via frankfurter.dev"` still
attached. Verified paths in: unknown currency, a lowercase `to=try`, `EUR→EUR`,
connection refused, upstream 500, an HTML proxy page. `raise_for_status()` is
never called, so upstream error bodies land here too.

The model cannot see a failure, so it tells the customer **"250 EUR is 0.00
TRY."** Worse operationally: the 5xx rate stays at zero, so an outage is silent.

**Verify:** `curl -si ".../tools/convert?amount=250&from_=EUR&to=XYZ"` → `200 OK`,
`rate 0.0`. Same for `to=try` and `to=EUR`.

## The one I would fix before shipping tonight

**Delete line 60 (`rate = round(rate, 2)`)** and round only `result`.

It is not the worst finding per event — finding 1 produces 510% errors. I am
choosing on expected harm before morning: severity × how often it fires × how
risky the change is. It fires on **every** request, where finding 1 and the 404
fallback below both need a second call or an `on=` parameter. It reaches **−100%** for HUF, KRW and IDR
bases. And it is a one-line deletion: no new code path, nothing the calling
model sees changes, `git revert` if it surprises. Fixing the cache key or the
error contract changes what the model receives, and that is a supervised change
with a prompt update — not a 23:00 deploy.

## Also found

- **A 404 is treated as "weekend"** (lines 36-40). The upstream returns `404
  {"message":"not found"}` for a future date, a pre-1999 date or a bad code —
  valid JSON, so `payload.get("rates", {})` is empty and the code falls back to
  `/latest`. `on=2030-01-01` returns today's rate stamped `rate_date:
  "2030-01-01"`. A rate is invented for a day that has not happened.
- **`rate_date` is never read from the upstream** (lines 30, 44). It returns
  `str(on or date.today())` — the caller's own input echoed back;
  `payload["date"]` appears nowhere in the file although the upstream states it
  plainly. `asked_date` is missing entirely, so even a careful model cannot tell
  the customer which day the number is from.
- **The documented URL answers a different question** (lines 48-49). The
  parameters are `from_` and `on`, so `?from=USD&to=TRY&date=...` silently
  discards both and converts **EUR**→TRY at today's rate, reporting `"from":
  "EUR"`. The trailing underscore is forced — `from` is a keyword — but the
  missing `Query(alias="from")` is not.

## Things that look suspicious but are fine

- **"There is no timeout"** (line 23) would be wrong: `httpx.AsyncClient()`
  defaults to `Timeout(timeout=5.0)`. The real point is subtler — that 5s applies
  to *each phase*, not to the request as a whole. A drip-feeding upstream kept
  one request alive for **60 seconds** with no exception, and `fetch_rate` can
  issue two. The timeout exists; the total budget does not.
- **"The cache leaks memory"** (line 21) — it does not. The ECB publishes 30
  currencies, and invalid codes never reach the cache at all because the
  `KeyError` on line 42 fires before the write on line 43. Its problem is
  blindness to the date, not size.
- **"The module-level client is never closed"** (line 23) is tidiness, not a
  customer issue — sharing one client is the *correct* pattern, reusing the
  connection pool and TLS handshakes. The cost is a `ResourceWarning` at shutdown.
- **`round()`'s banker's rounding** is real and irrelevant: it moves the last
  cent at exact halves, which binary floats essentially never hit. Same for
  "should use `Decimal`" — measured difference 0.0000000000, against 21.80 from
  line 60.
