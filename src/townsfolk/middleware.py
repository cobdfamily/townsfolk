"""Request-ID middleware.

Every request gets an X-Request-ID -- either the
caller-supplied one (if they passed
`X-Request-ID: <uuid>`) or a freshly-minted UUID.
The id is:

  1. Echoed back in the response header so the
     caller can correlate.
  2. Attached to the request scope so handlers
     can log it.
  3. Pinned to a contextvar so any logger.info
     elsewhere in the call automatically carries
     it -- no need to thread the id through every
     function signature.

Caller-supplied ids are accepted as-is (we trust the
caller upstream of Traefik) but capped at 128 chars
to keep header sizes sane.

Why a contextvar rather than middleware-state: the
asyncpg pool and other async helpers don't have
access to the request object. A contextvar follows
the async task graph automatically, so
`logger.info(...)` from inside a db query sees the
right id.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


# Contextvar carries the current request id through
# the async task graph. Read from anywhere via
# `current_request_id()`. None outside a request.
_current_id: ContextVar[str | None] = ContextVar(
    "townsfolk_request_id", default=None,
)


def current_request_id() -> str | None:
    return _current_id.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Read/mint X-Request-ID; expose via contextvar +
    response header. Place this OUTSIDE every other
    middleware so the id is set before anything else
    runs."""

    def __init__(self, app, *, header: str = "X-Request-ID"):
        super().__init__(app)
        self.header = header

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(self.header, "")
        # Cap at 128 chars so a malicious header can't
        # blow up our log lines or downstream parsers.
        if incoming and len(incoming) <= 128:
            request_id = incoming
        else:
            request_id = uuid.uuid4().hex
        token = _current_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _current_id.reset(token)
        response.headers[self.header] = request_id
        return response


class RequestIdLogFilter(logging.Filter):
    """Attach the current request id to every log
    record. Wire this into the root logger via
    `logging.getLogger().addFilter(RequestIdLogFilter())`
    so any logger.info gets the id automatically -- no
    need to call `logger.info(..., extra={'rid': ...})`
    manually.

    Records outside a request carry rid='-' so log
    formatters don't have to handle None.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.rid = current_request_id() or "-"
        return True
