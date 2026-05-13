"""Request-ID middleware.

Reads the inbound ``X-Request-Id`` header and pins it on
``request.state.request_id`` (or generates a fresh UUID4 if
absent). Echoes the value back as a response header so the
caller can correlate logs without having to know the value
the gateway used.

The id flows into every ``chat.completion`` log event via
``contextvars`` so every line written during the same HTTP
request — including the structured outcome event in
``chat.py``, agent-loop tool-call rows, and any third-party
stdlib log emitted by ``litellm`` or ``httpx`` while
processing the request — carries the same ``request_id``.
That gives an operator a single grep that pulls the entire
turn out of ``gateway.log`` after the fact.

Pairs with the bot's ``X-Request-Id`` send (in
``gateway_client.py``) so a single id joins
``telegram-bot.log`` and ``gateway.log`` for the same chat
turn.

Header name: ``X-Request-Id``
-----------------------------

The de-facto-standard name used by load balancers, reverse
proxies, and tracing libraries (Envoy, NGINX
``$request_id``, AWS ALB ``X-Amzn-Trace-Id`` is a
superset). Using the standard name means a future ingress
proxy or sidecar will pass it through unchanged. We
deliberately do NOT use ``X-FITT-Request-Id`` here — that
prefix is reserved for FITT-specific metadata
(``X-FITT-Client``, ``X-FITT-Session``,
``X-FITT-Turn-Id``) where there is no industry standard.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Loose validation: 8-128 chars, ASCII letters/digits/dash/underscore.
# Tighter than RFC 7230 token rules but more than enough for any
# tracing system we'd realistically integrate with. The cap is
# defensive — a megabyte-long header is never a real request_id and
# the structured-log machinery doesn't need to write that to disk.
_VALID_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")

HEADER_NAME = "X-Request-Id"


def _new_request_id() -> str:
    """Generate a fresh request id.

    UUID4 (no dashes) is a 32-char hex string, well inside the
    validation regex's bounds and indistinguishable from a normal
    UUID for human readers."""
    return uuid.uuid4().hex


def _is_valid(candidate: str) -> bool:
    return bool(_VALID_REQUEST_ID_RE.match(candidate))


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Pin ``request.state.request_id`` from the inbound header
    or a freshly-generated UUID, echo it back in the response,
    and bind it to structlog's contextvars for the duration of
    the request.

    Order of precedence for the inbound id:

    1. Inbound ``X-Request-Id`` header that passes
       :func:`_is_valid`. Used verbatim.
    2. Anything else (header missing, malformed, too long,
       non-ASCII, ...): generate a fresh UUID. Logging the
       reason would be noisy on every health-check probe
       that doesn't bother sending the header, so we just
       silently substitute.

    Mounted in ``app.py`` outside the AuthMiddleware so even
    auth-rejected requests (401) appear in the log under a
    request_id, helping operators chase "the bot's token is
    wrong" symptoms.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(HEADER_NAME, "").strip()
        request_id = incoming if incoming and _is_valid(incoming) else _new_request_id()
        request.state.request_id = request_id

        # ``bound_contextvars`` adds keys to every structlog
        # event emitted from inside the ``with`` block, including
        # those written from background tasks spawned during the
        # request — agent-loop iterations, tool calls, fallback
        # dispatches. The key gets cleaned up automatically on
        # exit so other requests aren't tainted.
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            response = await call_next(request)

        response.headers[HEADER_NAME] = request_id
        return response
