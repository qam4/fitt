"""Bearer token authentication middleware.

Every ``/v1/*`` request must carry ``Authorization: Bearer <token>``
matching one of the tokens in ``secrets.yaml::allowed_tokens``.
Health-style endpoints are exempt so probes and monitoring don't need
the token.

Token comparison uses ``secrets.compare_digest`` to prevent timing
attacks — not strictly needed on a single-user Tailscale network, but
correct-by-default.

Client identity
---------------

Downstream handlers read ``request.state.client`` (one of ``ide``,
``telegram``, ``webui``, ``cli``) to route approvals and pick
per-client tool policies. The tag can come from two places:

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
  a logged warning — historical behaviour preserved for
  backward compatibility, but loud enough that operators notice.

Background on the principle: untagged tokens silently routing to
``webui`` was the root cause of a real bug where the Telegram bot
couldn't see approvals meant for it — the bot was polling
``?client=telegram`` but its requests came in tagged ``webui``.
We now support the bot declaring itself via header so a user
doesn't have to remember to tag tokens.
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

_VALID_CLIENTS: frozenset[str] = frozenset({"ide", "telegram", "webui", "cli", "coding-cli"})
"""Accepted values for the ``X-FITT-Client`` header and the
``client:`` field on tokens. Kept as a module constant so the
validation list doesn't drift between the config schema (which uses
``Literal[...]``) and the runtime check here.

``coding-cli`` (added 2026-05-11) marks clients that own their
own agent loop — Aider, Claude Code, Cursor's agent mode, Codex,
Kiro CLI, any coding CLI that expects a plain OpenAI-compatible
endpoint. The chat handler treats these as router-mode: FITT
resolves aliases, dispatches, tracks cost, audits; it does NOT
inject the capability block, does NOT merge FITT tools into the
request's ``tools`` array, does NOT inject memory, and does NOT
run the approval middleware. The client's own agent owns those
concerns. See :func:`is_router_mode_client` and the Aider-
collision entry in docs/observed-issues.md."""


def is_router_mode_client(client: str) -> bool:
    """Return ``True`` when ``client`` should get a thin-router
    pass-through rather than the full FITT agent layering.

    Single source of truth: any call site that wants to branch
    on "is this a coding-agent client?" reads this function,
    so a future client tag added for the same shape doesn't
    require hunting down parallel equality checks."""
    return client == "coding-cli"


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

    On success, stores the resolved client tag on
    ``request.state.client`` for downstream handlers to consult
    (approval routing, per-client policies). See module docstring
    for the header-vs-token resolution rules.
    """

    def __init__(self, app, config: Config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        # Store (token, optional_tag) so the header path can still
        # fall back to the tag when no header is sent.
        self._allowed: list[tuple[str, str | None]] = [
            (t.token, t.client) for t in (config.secrets.allowed_tokens if config.secrets else [])
        ]
        # Warn once at boot if any token is untagged. Untagged
        # tokens default to "webui" (least-trusted) at request
        # time, which is the safe choice but often not what the
        # operator meant. Typical symptom: the Telegram bot sends
        # chat requests that end up tagged "webui" and can't see
        # its own approvals. The X-FITT-Client header fixes this
        # for well-behaved clients; this warning nudges operators
        # to add the tag explicitly if they want the config to be
        # self-describing.
        for i, (_, tag) in enumerate(self._allowed):
            if tag is None:
                name = (
                    config.secrets.allowed_tokens[i].name
                    if config.secrets and config.secrets.allowed_tokens
                    else "?"
                )
                _log.warning(
                    "auth.token_without_client_tag",
                    extra={
                        "token_name": name,
                        "hint": (
                            "Add a `client:` field (ide, telegram, webui, "
                            "or cli) to this token in secrets.yaml. "
                            "Clients that send the X-FITT-Client header "
                            "are unaffected; this only matters for "
                            "clients that don't."
                        ),
                    },
                )

    def _is_exempt(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PREFIXES)

    def _match(self, header: str | None) -> tuple[bool, str | None]:
        """Return ``(ok, token_tag)``. ``ok`` is whether a bearer
        token matched; ``token_tag`` is its ``client:`` tag or
        ``None`` if the token was untagged."""
        if not header:
            return False, None
        parts = header.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False, None
        provided = parts[1].strip()
        if not provided:
            return False, None
        for allowed, tag in self._allowed:
            if secrets.compare_digest(provided, allowed):
                return True, tag
        return False, None

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._is_exempt(request.url.path):
            # No client tag on exempt paths — those shouldn't be
            # making tool-dispatching calls.
            return await call_next(request)

        ok, token_tag = self._match(request.headers.get("authorization"))
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
    """Combine the token's ``client:`` tag and the request's
    ``X-FITT-Client`` header into one effective client identity.

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
