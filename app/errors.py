"""The one way this service reports failure.

Every error the caller can see is an `FxError`: a machine-readable `code` the
calling model can branch on, and a `message` it can repeat to a customer. The
code carries the HTTP status with it so no route has to remember which is which.
"""

from __future__ import annotations

# A wrong number is worse than no number, so every one of these is a refusal to
# answer -- none of them is a fallback that still returns a rate.
ERROR_STATUS: dict[str, int] = {
    # The caller can fix these.
    "invalid_amount": 400,
    "invalid_currency": 400,
    "invalid_date": 400,
    "invalid_request": 400,
    "unknown_parameter": 400,
    "date_in_future": 400,
    "same_currency": 400,
    "unknown_endpoint": 404,
    "method_not_allowed": 405,
    # The request is well formed but no rate exists for it.
    "unknown_currency": 404,
    "no_rate_for_date": 404,
    "rate_unavailable": 404,
    # Our side or the provider's.
    "upstream_unavailable": 502,
    "upstream_error": 502,
    "upstream_invalid_response": 502,
    "upstream_timeout": 504,
    "internal_error": 500,
}


class FxError(Exception):
    """A failure worth telling the caller about."""

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_STATUS:
            raise KeyError(f"unknown error code: {code!r}")
        self.code = code
        self.message = message
        self.status_code = ERROR_STATUS[code]
        super().__init__(f"{code}: {message}")
