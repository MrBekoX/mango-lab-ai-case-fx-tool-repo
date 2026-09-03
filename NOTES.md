# Notes

## Decisions

**When the ECB published no rate for the day asked about, I answer — and say so.**
The provider already falls back to the previous business day, and it tells you
the truth in its `date` field. So `rate_date` is always that field, verbatim,
and `asked_date` is what the caller sent; when they differ the model can see it
and say "that is Friday's rate". I did try an extra boolean saying the same
thing, and took it back out — the brief's example response is eight fields and
it already names those two dates as where the difference shows. Refusing
weekends outright was the other option; it would have made the tool useless two
days in seven for no gain.

**The subtlety that nearly got me:** when no `date` is sent, the obvious move is
`asked_date = rate_date`. That makes the two dates always agree on the commonest
call there is, so a Saturday "what's the rate now?" would present a two-day-old
number as current with nothing to notice. `asked_date` is today instead.

**I don't trust the payload, I check it.** Before reading `rates[to]` the client
confirms the provider answered the question actually asked: the base currency
matches, rates are quoted per one unit, the requested currency is present, the
rate is finite and positive, the date is real and not later than the day asked
about. Any of these failing is a 502, not a number. Without the `base` check, an
upstream that ignores the parameter returns EUR-based rates and the service
reports them as TRY-based — a 200 with a number wrong by a factor of twenty.

**`from == to` is a 400, not `1.0`.** Answering would put a rate in a 200 that no
ECB publication stands behind, and give it a date. The provider rejects the pair
too (422). The message says the amount is unchanged, so the model can still
answer the customer.

**No host is written in the code at all.** The base URL is read from the
environment, then `.env`, then the committed `.env.example` that carries the
documented default — one function in `app/config.py`, so "nothing hardcodes the
host" is a claim you can check in one place. The environment wins over both
files, because a stale `.env` silently redirecting the service away from the
upstream someone pointed it at is exactly the class of failure this thing exists
to prevent.

**The `/v1` prefix is discovered, not assumed.** The brief's default base
(`https://api.frankfurter.dev`) 404s on its own — the real API lives under `/v1`.
Hardcoding the prefix would break against a fake upstream serving at the root;
hardcoding nothing would break against the real one. So it tries `/v1`, falls
back to the root, and remembers. I tested both, plus a closed port.

**Money never touches float.** `amount` parses straight to `Decimal`, the
provider's body is parsed with `parse_float=Decimal`, and only the final JSON
encoding converts. `rate` is passed through untouched.

## With another day

Currency-aware rounding (`result` is 2 decimals for everything, which is wrong
for JPY). Single-flight, so the "doesn't re-ask" guarantee holds under
concurrency and not just sequentially. One retry on a connection failure.
Structured logging with a request id. And I'd ask you two questions I had to
guess at instead: whether your fake upstream serves at the root or under `/v1`,
and whether you expect a 200 or an error for `from == to`.

## AI tools

Claude Code, heavily — the brief invited it, so I used it the way I actually
work rather than pretending otherwise. What made it useful was not asking it to
write the service. It was:

- **Measuring instead of assuming.** Before designing anything I made real calls
  to frankfurter and wrote down what it does. That is where I learned a future
  date returns 404 rather than the latest rate, and that the root path 404s.
- **Adversarial review of the design before writing code.** I had it attack my
  own design from three angles: does it meet the brief, are the framework
  assumptions actually true on these versions, and can I make it return a
  confidently wrong number. The third one found the `asked_date` bug above and
  the missing payload checks, while the whole thing was still a document.
- **Verifying framework behaviour on the installed versions**, not from memory.
  Which is how I knew to register the error handler on Starlette's
  `HTTPException` rather than FastAPI's — otherwise a typo'd URL answers with
  `{"detail": "Not Found"}` and never reaches my error shape.

I wrote the tests myself first in most cases, because a test is where I find out
whether I actually understood the requirement.

## One thing the AI got wrong

The handler was written as `async def convert(...) -> dict:`. FastAPI takes that
return annotation as a response model, and Pydantic v2 serialises `Decimal` as a
JSON **string** — so `rate` came back as `"56.1718"`, quoted. The whole point of
keeping `Decimal` end to end was undone at the last step, and the brief's own
example shows a bare number.

I found it because a test asserted `isinstance(raw["rate"], float)` on the parsed
body rather than just comparing values — `"56.1718" == 56.1718` is false, but a
laxer assertion like `float(body["rate"]) == 56.1718` would have passed and hidden
it.

What stings is that I *had* checked this specific risk before writing the code.
I ran a small script to confirm a plain `dict` return serialises `Decimal` as a
number — and it does. My script's endpoint just had no return annotation. Right
question, wrong setup. The fix is `response_model=None`, with a comment saying
why so nobody helpfully adds the annotation back.
