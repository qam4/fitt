"""Bearer token authentication middleware.

Every ``/v1/*`` request must carry ``Authorization: Bearer <token>``
matching one of the tokens in ``secrets.yaml::allowed_tokens``.
Health-style endpoints are exempt so probes and monitoring don't need
the token.

Token comparison uses ``secrets.compare_digest`` to prevent timing
attacks — not strictly needed on a single-user Tailscale network, but
correct-by-default.

Two attributes ride on every authenticated request:

- ``request.state.client``: which interface initiated the call
  (``ide``, ``telegram``, ``webui``, ``cli``). Drives per-client
  approval defaults, approval routing, audit / observability tags.
- ``request.state.mode``: ``router`` or ``agent``. Router mode
  treats FITT as a thin alias-routing proxy — no capability block
  injection, no FITT tool merge, no memory injection, no approval
  middleware. Agent mode (the default) runs the full FITT
  layering. Router mode exists for clients that own their own
  agent loop (Aider, OpenCode, Claude Code, Cursor agent mode,
  Codex, Kiro CLI). See ``docs/coding-cli-setup.md`` for setup
  patterns and ``docs/observed-issues.md`` for the Aider
  collision that motivated it.

Client identity resolution
--------------------------

The ``client`` tag can come from two places:

1. **`X-FITT-Client` request header** — the self-assertion the
   client sends. The Telegram bot always sends
   ``X-FITT-Client: telegram``.
2. **``client:`` field on the token** in ``secrets.yaml`` — the
   operator-set default.

Resolution order, in priority:

* If the header is present, it wins. If the token also has a tag
  and they disagree, we reject with 400 ("client mismatch") so
  a compromised token can't silently misclaim.
* If only the token is tagged, use that.
* If neither is set, fall back to ``webui`` (least-trusted) with
  a logged warning.

Mode resolution
---------------

The ``mode`` field rides on the token only — it's a property of
"how this principal wants FITT to behave," set by the operator
when the token is issued. There's no header equivalent; a
request can't request router mode without an operator-issued
router-mode token. Two routes resolve to ``router``:

1. Token explicitly tagged ``mode: router`` (the recommended
   shape going forward).
2. Legacy tokens tagged ``client: coding-cli``. Kept for
   backward compatibility with operator setups that predate the
   ``mode`` field; resolves to ``client: ide, mode: router``.
   The boot-time deprecation warning in :func:`AuthMiddleware`
   tells the operator to migrate.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Config

_log = logging.getLogger(__name__)

_EXEMPT_PREFIXES: tuple[str, ...] = ("/health", "/ready", "/v1/models")

_VALID_CLIENTS: frozenset[str] = frozenset({"ide", "telegram", "webui", "cli"})
"""Accepted values for the ``X-FITT-Client`` header. The
``client:`` field on a token also accepts ``coding-cli`` as a
deprecated synonym for ``ide`` + ``mode: router``; see
:meth:`gateway.config.Secrets.client_for` and
:meth:`gateway.config.Secrets.mode_for`. The deprecated value
is NOT accepted on the header — operators who want router
mode declare it via the token, not the request."""


def is_router_mode_request(request: Request) -> bool:
    """Return ``True`` when this request should get a thin-router
    pass-through rather than the full FITT agent layering.

    Reads ``request.state.mode`` populated by
    :class:`AuthMiddleware`. Single source of truth so call
    sites in chat.py and elsewhere don't reproduce the
    "is this a coding-agent request?" check by hand.

    Defaults to ``False`` if ``mode`` isn't set — defensive
    posture for a hypothetical caller path that bypasses auth
    middleware (tests that build a request fixture directly,
    say). The agent path is the safe default."""
    return getattr(request.state, "mode", "agent") == "router"


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


def _bad_request(message: str) -> JSONResponse:
    """400 for client-identity conflicts (header disagrees with
    token tag, or header value is unrecognised). Distinct from 401
    so the operator can tell "auth is fine, configuration is wrong"
    from the logs."""
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "auth_error",
                "code": "client_mismatch",
            }
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce Bearer-token auth on non-exempt paths.

    On success, stores two values on ``request.state``:

    * ``client``: resolved interface tag (header / token tag /
      fallback). See module docstring for resolution rules.
    * ``mode``: ``"router"`` or ``"agent"``. Read from the
      matched token via :meth:`Secrets.mode_for`.

    Downstream handlers consult these for approval routing,
    per-client policies, and the router-mode pass-through
    branch.
    """

    def __init__(self, app, config: Config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        # Store (token, client_tag, mode) so the dispatch loop
        # doesn't need to re-resolve from the secrets object on
        # every request.
        self._allowed: list[tuple[str, str | None, str]] = []
        if config.secrets is not None:
            for entry in config.secrets.allowed_tokens:
                client_tag = entry.client
                # Legacy compat: coding-cli token resolves to
                # client=ide. The mode comes from the dedicated
                # field, falling back to "router" if the legacy
                # tag is what brought them here.
                if client_tag == "coding-cli":
                    resolved_client_tag: str | None = "ide"
                    resolved_mode = entry.mode or "router"
                else:
                    resolved_client_tag = client_tag
                    resolved_mode = entry.mode or "agent"
                self._allowed.append((entry.token, resolved_client_tag, resolved_mode))

        # Warn once at boot if any token is untagged. Untagged
        # tokens default to "webui" (least-trusted) at request
        # time, which is the safe choice but often not what the
        # operator meant.
        if config.secrets is not None:
            for entry in config.secrets.allowed_tokens:
                if entry.client is None:
                    _log.warning(
                        "auth.token_without_client_tag",
                        extra={
                            "token_name": entry.name,
                            "hint": (
                                "Add a `client:` field (ide, "
                                "telegram, webui, or cli) to this "
                                "token in secrets.yaml. Clients "
                                "that send the X-FITT-Client "
                                "header are unaffected; this only "
                                "matters for clients that don't."
                            ),
                        },
                    )

            # Deprecation warning for any legacy ``coding-cli``
            # tags. Don't break the boot — the runtime
            # transparently maps the value to
            # ``client: ide, mode: router`` — but tell the
            # operator how to migrate.
            legacy = config.secrets.legacy_coding_cli_token_names()
            for name in legacy:
                _log.warning(
                    "auth.coding_cli_tag_deprecated",
                    extra={
                        "token_name": name,
                        "hint": (
                            "`client: coding-cli` is deprecated. "
                            "Replace with `client: ide` + "
                            "`mode: router` in secrets.yaml. The "
                            "old tag still works but will be "
                            "removed in a future release."
                        ),
                    },
                )

    def _is_exempt(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PREFIXES)

    def _match(self, header: str | None) -> tuple[bool, str | None, str]:
        """Return ``(ok, token_client_tag, mode)``. ``ok`` is
        whether a bearer token matched; ``token_client_tag`` is
        its resolved client tag (legacy ``coding-cli`` already
        mapped to ``ide``) or ``None`` if untagged; ``mode`` is
        the token's runtime mode (``"router"`` or ``"agent"``)
        with the legacy default applied for ``coding-cli``."""
        if not header:
            return False, None, "agent"
        parts = header.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False, None, "agent"
        provided = parts[1].strip()
        if not provided:
            return False, None, "agent"
        for allowed, client_tag, mode in self._allowed:
            if secrets.compare_digest(provided, allowed):
                return True, client_tag, mode
        return False, None, "agent"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._is_exempt(request.url.path):
            # No client / mode on exempt paths — those shouldn't
            # be making tool-dispatching calls.
            return await call_next(request)

        ok, token_tag, mode = self._match(request.headers.get("authorization"))
        if not ok:
            return _unauthorized("Missing or invalid Bearer token.")

        header_tag = request.headers.get("x-fitt-client")
        if header_tag is not None:
            header_tag = header_tag.strip().lower()
            if header_tag not in _VALID_CLIENTS:
                return _bad_request(
                    f"X-FITT-Client header value {header_tag!r} is not one of "
                    f"{sorted(_VALID_CLIENTS)}."
                )

        effective = _resolve_client(token_tag=token_tag, header_tag=header_tag or None)
        if isinstance(effective, _MismatchError):
            return _bad_request(
                f"X-FITT-Client header says {effective.header_tag!r} but the "
                f"token is tagged {effective.token_tag!r}. Either remove the "
                f"header, remove the token's `client:` field, or make them "
                f"agree."
            )

        request.state.client = effective
        request.state.mode = mode
        return await call_next(request)


class _MismatchError:
    """Internal signal that the header and token tag disagree.

    Not an exception: the auth dispatcher expects to map this to a
    400 response with both values visible, which is easier with a
    tiny dataclass than a raised-and-caught exception."""

    __slots__ = ("header_tag", "token_tag")

    def __init__(self, header_tag: str, token_tag: str) -> None:
        self.header_tag = header_tag
        self.token_tag = token_tag


def _resolve_client(*, token_tag: str | None, header_tag: str | None) -> str | _MismatchError:
    """Combine the token's resolved ``client:`` tag and the
    request's ``X-FITT-Client`` header into one effective client
    identity.

    Precedence:

    * Header + matching token tag → header (they agree)
    * Header + disagreeing token tag → :class:`_MismatchError`
    * Header only → header
    * Token tag only → token tag
    * Neither → ``"webui"`` (least-trusted default)
    """
    if header_tag is not None and token_tag is not None:
        if header_tag == token_tag:
            return header_tag
        return _MismatchError(header_tag=header_tag, token_tag=token_tag)
    if header_tag is not None:
        return header_tag
    if token_tag is not None:
        return token_tag
    return "webui"
