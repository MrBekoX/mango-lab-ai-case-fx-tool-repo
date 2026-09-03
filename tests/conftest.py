"""Test doubles for the rate provider.

Nothing in this suite touches the network. Every request is answered by an
`httpx.MockTransport`, so the tests pass with `FX_UPSTREAM_BASE` pointing at a
closed port -- which is exactly how they are meant to be run.
"""

import asyncio
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, get_upstream
from app.upstream import Upstream

BASE = "http://fake-upstream.test"

FRIDAY = "2026-08-28"
SATURDAY = "2026-08-29"
TODAY = "2026-09-03"

NOT_FOUND = (404, '{"message":"not found"}')
CURRENCIES = (
    200,
    '{"EUR":"Euro","TRY":"Turkish Lira","USD":"US Dollar","JPY":"Japanese Yen"}',
)


def rates_body(*, base="EUR", quote="TRY", rate="56.1718", on=FRIDAY, amount="1.0"):
    """Raw JSON text, so a test can control the exact literal that goes on the
    wire -- including ones `json.dumps` would never produce."""
    return (
        f'{{"amount":{amount},"base":"{base}","date":"{on}",'
        f'"rates":{{"{quote}":{rate}}}}}'
    )


def transport_for(routes, default=NOT_FOUND):
    """A MockTransport that answers from `routes` and records every request.

    A route value may be an exception instance, which is raised instead of
    answered -- that is how connection and timeout failures are simulated.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        outcome = routes.get(request.url.path, default)
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return httpx.Response(
            status, content=body, headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler), calls


def make_upstream(routes, *, default=NOT_FOUND, base=BASE):
    transport, calls = transport_for(routes, default)
    return Upstream(httpx.AsyncClient(transport=transport), base=base), calls


def rate_calls(calls):
    """The calls that asked for a rate, ignoring the one-off discovery probe."""
    return [c for c in calls if not c.url.path.endswith("/currencies")]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(monkeypatch):
    """Build a TestClient whose provider is a MockTransport and whose idea of
    "today" is fixed, so no test depends on the wall clock or the network."""

    def build(routes=None, *, default=NOT_FOUND, base=BASE, now=TODAY):
        upstream, calls = make_upstream(routes or {}, default=default, base=base)
        monkeypatch.setattr("app.main.today", lambda: date.fromisoformat(now))
        app.dependency_overrides[get_upstream] = lambda: upstream
        return TestClient(app, raise_server_exceptions=False), calls

    yield build
    app.dependency_overrides.clear()
