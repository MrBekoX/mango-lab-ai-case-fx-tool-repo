# Review of tool.py

It runs, and that is the problem: almost everything that goes wrong here comes
back as `HTTP 200` with a number in it. Ranked by what reaches a customer.

## 1. The cache ignores the date, so one answer is sold as every date — lines 21, 28-30, 43

`key = f"{base}-{target}"`. The date is in neither the key nor the value, there
is no TTL, and on a hit the response is labelled with **whatever date the caller
asked for** (line 30).

```
GET ...&on=2026-01-02   → asks upstream, rate 47.12, rate_date 2026-01-02
GET ...                 → no upstream call. rate 47.12, rate_date "today"
GET ...&on=2020-06-15   → no upstream call. rate 47.12, rate_date "2020-06-15"
```

January's rate is sold first as today's, then as June 2020's. EUR/TRY was ~7.7
in June 2020, so that last answer is out by **510%**, and nothing in the response
hints at it — `rate_date` says exactly what the caller asked for. One user's
historical lookup poisons every later user's "current" rate for the life of the
process. Under `--workers 4` the same question gets different answers depending
on which worker takes it.

**Verify:** run the three calls above and watch `rate` never change;
`python -c "import tool; print(tool._cache)"` shows a single key, no date.

## 2. The rate is rounded to two decimals *before* the multiplication — lines 60-61

Not a display detail: line 61 multiplies by the damaged value. Real ECB rates,
2026-09-03, for 1,000,000 units:

| Pair | Real rate | Used | Correct | Told to the customer |
|---|---|---|---|---|
| HUF→EUR | 0.00272 | **0.0** | 2,720.00 | **0.00** |
| KRW→EUR | 0.00063 | **0.0** | 630.00 | **0.00** |
| JPY→EUR | 0.00552 | 0.01 | 5,520.00 | **10,000.00** (+81%) |
| TRY→EUR | 0.01782 | 0.02 | 17,820.00 | **20,000.00** (+12.2%) |

Any base whose rate is under 0.005 converts to exactly **zero** — and that zero
is byte-identical to the error response in finding 3, so support cannot tell the
two apart afterwards. This needs no special input; it damages every successful
response.

**Verify:** `curl ".../tools/convert?amount=1000000&from_=JPY&to=EUR"` → `rate
0.01, result 10000.0`. `python -c "print(round(0.00552, 2))"` → `0.01`.

## 3. Every failure becomes `200 OK` with `rate: 0.0` — lines 71-81

The bare `except Exception` swallows everything, `print`s a line, and returns a
body that is structurally indistinguishable from success — `source: "ECB via
frankfurter.dev"` still attached. Verified paths into it: unknown currency, a
lowercase `to=try` (the upstream keys are upper-case), `EUR→EUR` (upstream 422),
connection refused, DNS failure, upstream 500 or an HTML proxy page, 429.

The model has no way to see a failure, so it tells the customer **"250 EUR is
0.00 TRY."** Operationally it is worse than it looks: the 5xx rate stays at zero,
so no alarm fires and no retry triggers — a full upstream outage is silent.
`response.raise_for_status()` is never called, so upstream 404 and 500 bodies
also land here.

**Verify:** `curl -si ".../tools/convert?amount=250&from_=EUR&to=XYZ"` → `200 OK`,
`rate 0.0`. Same for `to=try` and `to=EUR`.

## The one I would fix before shipping tonight

**Delete line 60 (`rate = round(rate, 2)`)** and let line 61 multiply the real
rate; round only `result`.

It is not the most harmful finding per event — finding 1 produces 510% errors
and finding 4 below invents rates for dates that do not exist. I am choosing on
expected harm before morning, which is severity × how often it fires × how
risky the change is:

- **It fires on every request.** Findings 1 and 4 need a second call or an `on=`
  parameter; this one damages the happy path, tonight, for everyone.
- **It reaches −100%.** HUF, KRW, IDR and similar bases return exactly 0.00.
- **It is a one-line deletion.** No new code path, no change to the response
  shape, `git revert` if anything surprises. Fixing the cache key or the error
  contract changes what the calling model receives, and that is a supervised
  change with a prompt update — not a 23:00 deploy.

If I could take a second line: delete the fallback at 36-40, add
`raise_for_status()`, and let the exception at 71 become a 502. That closes
findings 3 and 4 together, and it is still mostly deletion — but it changes the
error contract the model consumes, so it goes out in the morning with someone
watching.

## Also found

- **4. A 404 is treated as "weekend"** (lines 36-40). The upstream returns `404
  {"message":"not found"}` for a future date, a pre-1999 date or a bad code —
  valid JSON, so `payload.get("rates", {})` is empty and the code falls back to
  `/latest`. `on=2030-01-01` returns today's rate stamped `rate_date:
  "2030-01-01"`. A rate is invented for a day that has not happened.
- **5. `rate_date` is never read from the upstream** (lines 30, 44). It returns
  `str(on or date.today())` — the caller's own input echoed back. `payload["date"]`
  appears nowhere in the file, although the upstream states it plainly. Asked
  about Saturday, the upstream answers `"date": "2026-08-28"` and the service
  reports `2026-08-29`. `date.today()` is also the server's local date, so every
  morning before the ~16:00 CET publication is mislabelled too.
- **6. The documented URL answers a different question** (lines 48-49). The
  parameters are `from_` and `on`, so `?from=EUR&date=...` is silently discarded
  and the defaults apply. `?amount=250&from=USD&to=TRY` converts **EUR**→TRY and
  reports `"from": "EUR"` — about 16% off, with no error.
- **7. `asked_date` is missing entirely**, so even a careful model cannot tell a
  customer which day the number is from.
- **8. `amount` is unvalidated** (line 48). `nan`, `inf` and `1e400` all return
  `200` with `"amount": null, "result": null`; `-5` returns `-235.60`. A missing
  `amount` returns FastAPI's `{"detail": [...]}`, not the documented error shape.
- **9. No `FX_UPSTREAM_BASE`** (line 18). Beyond the brief: no way to fail over
  during an outage, and no way to test any of the above without the network —
  which is why this file has no tests.
- **10. No single-flight.** 20 simultaneous identical requests produced 20
  upstream calls; the fallback doubles that on the error path.

## Things that look suspicious but are fine

- **"There is no timeout"** (line 23) would be wrong: `httpx.AsyncClient()`
  defaults to `Timeout(timeout=5.0)`. The real point is subtler — that 5s applies
  to *each phase* (connect/read/write/pool), not to the request as a whole. A
  drip-feeding upstream kept one request alive for **60.2 seconds** with no
  exception, and `fetch_rate` can issue two of them. So: the timeout exists, the
  total budget does not.
- **"The module-level client is never closed"** (line 23) is a tidiness issue,
  not a customer one. Sharing one client across requests is the *correct*
  pattern — it reuses the connection pool and TLS handshakes. It construct fine
  with no running loop, and 8 concurrent requests on one loop all returned 200.
  The only cost is a `ResourceWarning` at shutdown.
- **"The cache leaks memory"** (line 21) — it does not. The ECB publishes 30
  currencies, so the key space tops out around 870 entries, and invalid codes
  never get cached at all because the `KeyError` on line 42 fires before the
  write on line 43. Its problem is blindness to the date, not size.
- **`from __future__ import annotations` with FastAPI** (line 9) works: on
  current versions `date | None` still parses, rejects bad input with 422, and
  produces the right OpenAPI schema.
- **`round()`'s banker's rounding** is real and irrelevant here — it moves the
  last cent at exact halves, which binary floats essentially never hit (0
  occurrences in 200k samples). It is noise beside the early rounding in
  finding 2. Same for "should use `Decimal`": measured difference 0.0000000000,
  against 21.80 from line 60.
- **The trailing underscore in `from_`** is not a typo. `from` is a Python
  keyword; the underscore is forced. The defect is the missing
  `Query(alias="from")`, not the name.
- **User input reaching `params=`** is not an injection risk: httpx
  percent-encodes it, and the only value on the path is a parsed `date`.
