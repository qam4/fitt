"""Bearer token authentication middleware.

Every ``/v1/*`` request must carry ``Authorization: Bearer <token>``
matching one of the tokens in ``secrets.yaml::allowed_tokens``.
Health-style endpoints are exempt so probes and monitoring don't need
the token.

Token comparison uses ``secrets.compare_digest`` to prevent timing
attacks — not strictly needed on a single-user Tailscale network, but
correct-by-default.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Config

_EXEMPT_PREFIXES: tuple[str, ...] = ("/health", "/ready", "/v1/models")


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": message,
                "type": "auth_error",
                "code": "unauthorized",
            }
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce Bearer-token auth on non-exempt paths."""

    def __init__(self, app, config: Config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._allowed = [t.token for t in (config.secrets.allowed_tokens if config.secrets else [])]

    def _is_exempt(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PREFIXES)

    def _check(self, header: str | None) -> bool:
        if not header:
            return False
        parts = header.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        provided = parts[1].strip()
        if not provided:
            return False
        # Constant-time comparison against every allowed token.
        for allowed in self._allowed:
            if secrets.compare_digest(provided, allowed):
                return True
        return False

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._is_exempt(request.url.path):
            return await call_next(request)
        header = request.headers.get("authorization")
        if not self._check(header):
            return _unauthorized("Missing or invalid Bearer token.")
        return await call_next(request)
