"""The HTTP surface of the conversion tool.

Owns the request contract and the money arithmetic. Every rate and every date in
a 200 response comes from the provider's payload -- nothing here derives either
one, and every failure is a non-2xx refusal rather than a plausible-looking
number.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BeforeValidator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from .errors import FxError
from .upstream import TIMEOUT, Upstream

logger = logging.getLogger("fx")

# Where the numbers come from. This is a provenance label for the caller, not
# the address we fetch from, so it does not follow $FX_UPSTREAM_BASE.
SOURCE = "ECB via frankfurter.dev"

MAX_AMOUNT = Decimal("1e12")
CENTS = Decimal("0.01")
KNOWN_PARAMS = frozenset({"amount", "from", "to", "date"})

# The ECB publishes on Frankfurt time, so that is the calendar a date is
# compared against.
TZ_NAME = "Europe/Berlin"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        app.state.upstream = Upstream(client)
        yield


app = FastAPI(title="fx-tool", version="1.0", lifespan=lifespan)


def get_upstream(request: Request) -> Upstream:
    """Reached through a dependency rather than read from app.state directly, so
    a test can substitute a provider that never touches the network."""
    return request.app.state.upstream


def today() -> date:
    """Falling back to UTC keeps the service running on an image with no
    timezone database. The worst case is an hour's disagreement around midnight,
    and even then the provider still decides which day its rate belongs to."""
    try:
        tz = ZoneInfo(TZ_NAME)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.now(tz).date()


def _normalise(value):
    return value.upper() if isinstance(value, str) else value


# Upper-cased before the pattern runs, so `eur` is accepted and `EURO` is not.
Currency = Annotated[
    str,
    BeforeValidator(_normalise),
    Query(pattern="^[A-Za-z]{3}$", description="ISO 4217 code, e.g. EUR"),
]


# response_model=None matters: FastAPI would otherwise take the `-> dict`
# annotation as a response model, and Pydantic serialises Decimal as a JSON
# *string*. The brief's example shows bare numbers, and a model reading
# "rate": "56.1718" has to unquote it before it can do arithmetic.
@app.get("/tools/convert", response_model=None)
async def convert(
    request: Request,
    amount: Annotated[
        Decimal,
        Query(gt=0, le=MAX_AMOUNT, allow_inf_nan=False, description="How much to convert"),
    ],
    from_: Annotated[Currency, Query(alias="from")],
    to: Currency,
    upstream: Annotated[Upstream, Depends(get_upstream)],
    on: Annotated[
        date | None,
        Query(alias="date", description="YYYY-MM-DD; omit for the latest published rate"),
    ] = None,
) -> dict:
    # A caller that sends `on=` instead of `date=` has asked about a specific day.
    # Silently ignoring it would answer a different question with no sign of it.
    unknown = sorted(set(request.query_params) - KNOWN_PARAMS)
    if unknown:
        raise FxError(
            "unknown_parameter",
            f"This endpoint does not accept {', '.join(unknown)}. "
            f"It takes amount, from, to and an optional date.",
        )

    if from_ == to:
        # Answering 1.0 would mean putting a rate in the response that no ECB
        # publication stands behind, and dating it. Better to say so.
        raise FxError(
            "same_currency",
            f"{from_} and {to} are the same currency, so there is no ECB rate to "
            f"quote; the amount is unchanged.",
        )

    now = today()
    asked = on or now
    if asked > now:
        raise FxError(
            "date_in_future",
            f"{asked.isoformat()} is in the future; the ECB has not published a rate for it.",
        )

    rate, rate_date = await upstream.get_rate(from_, to, on, now.isoformat())

    try:
        result = (amount * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError):
        raise FxError(
            "invalid_amount", "That amount is too large to convert precisely."
        ) from None

    asked_date = asked.isoformat()
    return {
        "amount": amount,
        "from": from_,
        "to": to,
        "rate": rate,
        "result": result,
        # The day the rate belongs to, straight from the provider...
        "rate_date": rate_date,
        # ...and the day the caller asked about. When they differ, the caller is
        # holding an older publication and has to be able to say so.
        "asked_date": asked_date,
        "rate_is_from_earlier_date": rate_date < asked_date,
        "source": SOURCE,
    }


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# -- error responses -------------------------------------------------------


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


@app.exception_handler(FxError)
async def _fx_error(request: Request, exc: FxError) -> JSONResponse:
    return _error(exc.code, exc.message, exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    code, message = _describe(exc)
    return _error(code, message, 400)


@app.exception_handler(StarletteHTTPException)
async def _routing_error(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    # Registered on Starlette's HTTPException, not FastAPI's: FastAPI's is a
    # subclass, so handling only that one would let the router's own 404 and 405
    # answer with FastAPI's default {"detail": ...} body instead of ours.
    code = _ROUTING_CODES.get(exc.status_code)
    if code is None:
        code = "internal_error" if exc.status_code >= 500 else "invalid_request"
    return _error(code, _ROUTING_MESSAGES.get(exc.status_code, str(exc.detail)), exc.status_code)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Without this the framework answers with plain text, which is neither JSON
    # nor the {error, message} shape every other failure uses. The caller gets
    # nothing useful out of a 500, so the detail has to survive in the log.
    logger.exception("unhandled error serving %s", request.url.path)
    return _error(
        "internal_error",
        "The service hit an unexpected error and did not produce a rate.",
        500,
    )


_ROUTING_CODES = {404: "unknown_endpoint", 405: "method_not_allowed"}
_ROUTING_MESSAGES = {
    404: "No such endpoint. This service exposes GET /tools/convert.",
    405: "That method is not allowed here; /tools/convert answers GET.",
}

_FIELD_CODE = {
    "amount": "invalid_amount",
    "from": "invalid_currency",
    "to": "invalid_currency",
    "date": "invalid_date",
}

# Checked in this order so a request with several problems always names the same
# one, instead of whichever the validator happened to list first.
_FIELD_ORDER = ("amount", "from", "to", "date")

_AMOUNT_MESSAGES = {
    "missing": "amount is required.",
    "decimal_parsing": "amount must be a number, for example 250 or 250.75.",
    "greater_than": "amount must be greater than zero.",
    "finite_number": "amount must be a finite number.",
    "less_than_equal": f"amount must not exceed {MAX_AMOUNT:f}.",
}


def _describe(exc: RequestValidationError) -> tuple[str, str]:
    problems: dict[str, str] = {}
    for error in exc.errors():
        problems.setdefault(str(error["loc"][-1]), error["type"])
    for field in _FIELD_ORDER:
        if field in problems:
            return _FIELD_CODE[field], _explain(field, problems[field])
    return "invalid_request", "The request could not be understood."


def _explain(field: str, kind: str) -> str:
    if field == "amount":
        return _AMOUNT_MESSAGES.get(kind, "amount is not a usable number.")
    if field == "date":
        return "date must be a calendar date in YYYY-MM-DD form."
    if kind == "missing":
        return f"{field} is required."
    return f"{field} must be a three-letter currency code, for example EUR."
