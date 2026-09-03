# Notes

## Decisions

**When the ECB published no rate for the day asked about, I answer — and say so.**
The provider already falls back to the previous business day and tells the truth
in its `date` field, so `rate_date` is that field verbatim and `asked_date` is
what the caller sent. When they differ the model can see it and say "that is
Friday's rate". I tried an extra boolean saying the same thing and took it back
out: the brief's example is eight fields, and it already names those two dates
as where the difference shows.

**The subtlety that nearly got me:** with no `date` given, the obvious move is
`asked_date = rate_date`. That makes the two agree on the commonest call there
is, so a Saturday "what's the rate now?" would present a two-day-old number as
current with nothing to notice. `asked_date` is today instead.

**I don't trust the payload, I check it.** Before reading `rates[to]`: the base
currency matches, rates are quoted per one unit, the requested currency is
present, the rate is finite and plausible, the date is real and not later than
the day asked about. Each failure is a 502, not a number. Without the `base`
check, an upstream that ignores the parameter returns EUR-based rates and we
report them as TRY-based — a 200 wrong by a factor of twenty.

**`from == to` is a 400, not `1.0`,** because answering would put a rate in a 200
that no ECB publication stands behind, and date it. The message says the amount
is unchanged, so the model can still answer.

**No host is written in the code.** The base URL comes from the environment,
then `.env`, then the committed `.env.example` — one function in
`app/config.py`, so "nothing hardcodes the host" is checkable in one place. The
environment wins over both files, because a stale `.env` redirecting the service
away from the upstream someone pointed it at is exactly what this thing exists
to prevent. The trade-off: with `.env.example` deleted the service refuses to
start rather than guessing a host. I preferred a loud failure to a literal.

**The `/v1` prefix is discovered, not assumed** — the brief's default base 404s
on its own, but hardcoding `/v1` would break a fake upstream serving at the
root. It tries both and remembers. **Money never touches float:** `Decimal` from
the query string, through `parse_float=Decimal` on the wire, to the final encode.

## With another day

Currency-aware rounding (`result` is 2 decimals for everything, wrong for JPY).
Single-flight, so "doesn't re-ask" holds under concurrency too. One retry on a
connection failure. Refreshing the currency list, which is currently cached for
the life of the process. And I would ask you the two questions I had to guess
at: whether your fake upstream serves at the root or under `/v1`, and whether
you expect a 200 or an error for `from == to`.

## AI tools

Claude Code, heavily — the brief invited it, so I used it the way I actually
work. What made it useful was not asking it to write the service:

- **Measuring instead of assuming.** I made real calls to frankfurter before
  designing anything. That is how I learned a future date returns 404 rather
  than the latest rate, and that the root path 404s — both of which changed the
  design.
- **Attacking my own design before writing code**, from three angles: does it
  meet the brief, are the framework assumptions true on these versions, and can
  I make it return a confidently wrong number. The third found the `asked_date`
  bug above and the missing payload checks while it was still a document.

I wrote the tests first in most cases, because a test is where I find out
whether I actually understood the requirement.

## One thing the AI got wrong

The handler was written as `async def convert(...) -> dict:`. FastAPI takes that
return annotation as a response model, and Pydantic v2 serialises `Decimal` as a
JSON **string** — so `rate` came back as `"56.1718"`, quoted. Keeping `Decimal`
end to end was undone at the last step, and the brief's example shows a bare
number.

A test caught it because it asserted `isinstance(raw["rate"], float)` on the
parsed body rather than comparing values; a laxer `float(body["rate"]) ==
56.1718` would have passed and hidden it.

What stings is that I *had* checked this exact risk beforehand. I ran a small
script to confirm a plain `dict` return serialises `Decimal` as a number — and
it does. My script's endpoint just had no return annotation. Right question,
wrong setup. The fix is `response_model=None`, with a comment saying why so
nobody helpfully puts the annotation back.
