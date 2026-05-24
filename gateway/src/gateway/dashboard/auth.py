"""Cookie-or-bearer auth for the dashboard.

Two paths into a dashboard route, in this order:

1. ``Authorization: Bearer <token>`` matching one of the
   tokens in ``secrets.allowed_tokens``. Same machinery the
   global :class:`gateway.auth.AuthMiddleware` uses for
   ``/v1/*`` — convenient for ``curl`` from the tailnet and
   for tools like Raycast widgets that hit dashboard JSON
   endpoints.
2. A signed session cookie named ``fitt_dashboard``, issued
   by ``/dashboard/login`` after the operator pasted a
   bearer token. The cookie is signed with HMAC-SHA256 over
   ``b"<expiry>|<client>"`` keyed on
   ``$FITT_HOME/dashboard.key`` (0600, generated on first use,
   same posture as ``audit.key``). 24h expiry baked in so a
   stolen cookie can't outlive a working day.

Why a separate key file: the bearer token is a long-lived
secret stored in ``secrets.yaml``. Reusing it as the HMAC key
would conflate "thing the operator types" with "thing the
gateway signs cookies with" — rotating the bearer token
would silently invalidate every active dashboard session in
addition to the new requirement to log back in. A dedicated
key keeps the two concerns separable.

Failure modes:

* No bearer header and no cookie → ``302 → /dashboard/login``
  with ``?next=<original_path>`` so the operator lands back
  where they tried to go.
* Bearer header present but invalid → ``401`` JSON envelope.
  No 302 here — a bad bearer is almost always a tool error,
  not a missing-login.
* Cookie present but signature mismatch / expired → ``302 →
  /dashboard/login``. The cookie is dropped on the redirect
  via a ``Set-Cookie`` with ``Max-Age=0``.

The :func:`require_auth` dependency is used by every
dashboard route. It's a Starlette/FastAPI dependency rather
than middleware so the dashboard's redirect semantics don't
leak into the global ``/v1/*`` request flow.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

_log = logging.getLogger(__name__)


COOKIE_NAME = "fitt_dashboard"
"""Name of the session cookie. Distinct from any bearer-token
header so the two auth paths don't conflict."""

COOKIE_TTL_SECONDS = 24 * 3600
"""Cookie lifetime. 24 hours is short enough that a stolen
cookie loses utility quickly, long enough that the operator
isn't typing a token into the login form every hour."""

KEY_BYTES = 32
"""Length of the HMAC key. 256 bits, same as
:mod:`gateway.audit`."""


# --------------------------------------------------------------- key file


def default_key_path(fitt_home: Path) -> Path:
    """Conventional path for the dashboard signing key under
    FITT_HOME. Mirrors :func:`gateway.audit.default_audit_paths`."""
    return fitt_home / "dashboard.key"


def load_or_generate_key(key_path: Path) -> bytes:
    """Return the dashboard signing key, generating it on first
    use. 0600 perms (POSIX) so only the gateway user can read;
    Windows defaults to the user-only ACL, which is equivalent
    on a single-user install. Lazy generation matches the audit
    key's posture — a gateway that never serves the dashboard
    doesn't leave a stray key file behind."""
    if key_path.exists():
        return key_path.read_bytes()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_bytes(KEY_BYTES)
    key_path.write_bytes(generated)
    if os.name != "nt":
        try:
            key_path.chmod(0o600)
        except OSError as exc:
            _log.warning(
                "dashboard.key_chmod_failed",
                extra={"path": str(key_path), "error": str(exc)},
            )
    _log.info("dashboard.key_generated", extra={"path": str(key_path)})
    return generated


# --------------------------------------------------------------- cookie codec


@dataclass(frozen=True, slots=True)
class CookiePayload:
    """The data we sign into the session cookie.

    ``expires_at`` is a UNIX timestamp; the cookie is
    rejected once ``time.time() >= expires_at``. ``client``
    is the resolved client tag (``ide``, ``telegram``,
    ``webui``, ``cli``, ``coding-agent``) — propagated to
    request state so dashboard routes can mirror the same
    per-client awareness the chat endpoints have.
    """

    expires_at: float
    client: str


def encode_cookie(payload: CookiePayload, *, key: bytes) -> str:
    """Encode a payload as ``<base64(payload)>.<base64(hmac)>``.

    Base64 (URL-safe, no padding) keeps the value cookie-clean.
    The HMAC is computed over the *encoded payload* — that's
    what the verifier reconstructs without trusting the
    payload bytes.
    """
    raw = f"{int(payload.expires_at)}|{payload.client}".encode()
    payload_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{payload_b64}.{sig_b64}"


def decode_cookie(cookie: str, *, key: bytes) -> CookiePayload | None:
    """Verify and decode. Returns ``None`` for any failure
    (malformed, signature mismatch, expired). All paths are
    timing-equivalent through ``hmac.compare_digest``; the
    explicit ``None`` is what the caller treats as 'no
    valid cookie'."""
    try:
        payload_b64, sig_b64 = cookie.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = base64.urlsafe_b64decode(_pad(sig_b64))
    except (ValueError, binascii.Error):
        return None
    if not hmac.compare_digest(expected, provided):
        return None

    try:
        raw = base64.urlsafe_b64decode(_pad(payload_b64)).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    try:
        expires_str, client = raw.split("|", 1)
        expires_at = float(expires_str)
    except ValueError:
        return None

    if time.time() >= expires_at:
        return None
    return CookiePayload(expires_at=expires_at, client=client)


def _pad(b64: str) -> str:
    """Re-pad URL-safe base64 to a multiple of 4. ``rstrip('=')``
    on encode means we have to ``+= '='`` here on decode."""
    return b64 + "=" * (-len(b64) % 4)


# --------------------------------------------------------------- bearer match


def _match_bearer(
    header: str | None, allowed: list[tuple[str, str | None]]
) -> tuple[bool, str | None]:
    """Mirror of :meth:`gateway.auth.AuthMiddleware._match`.

    Kept here as a small dedicated function so the dashboard
    doesn't reach into AuthMiddleware's internals. Returns
    ``(matched, client_tag)`` where ``client_tag`` is the
    token's ``client:`` field or ``None`` if the token is
    untagged."""
    if not header:
        return False, None
    parts = header.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False, None
    provided = parts[1].strip()
    if not provided:
        return False, None
    for token, tag in allowed:
        if secrets.compare_digest(provided, token):
            return True, tag
    return False, None


# --------------------------------------------------------------- dependency


class DashboardAuth:
    """Per-app auth context for the dashboard.

    Built once at app construction (so the bearer-token list
    is stable for the gateway's lifetime, matching the
    AuthMiddleware's posture) and stored on
    ``app.state.dashboard_auth``. The route dependency
    :func:`require_auth` reads it back from app state.
    """

    def __init__(
        self,
        *,
        allowed_bearers: list[tuple[str, str | None]],
        key_path: Path,
    ) -> None:
        self._allowed = allowed_bearers
        self._key_path = key_path
        self._key: bytes | None = None

    def key(self) -> bytes:
        """Lazy-load the signing key on first use. Failing to
        read or create the key would prevent any login from
        succeeding; we let the OSError propagate so a
        misconfigured filesystem fails loud."""
        if self._key is None:
            self._key = load_or_generate_key(self._key_path)
        return self._key

    def issue_cookie(self, *, client: str) -> str:
        payload = CookiePayload(
            expires_at=time.time() + COOKIE_TTL_SECONDS,
            client=client,
        )
        return encode_cookie(payload, key=self.key())

    def verify_cookie(self, cookie: str) -> CookiePayload | None:
        return decode_cookie(cookie, key=self.key())

    def match_bearer(self, header: str | None) -> tuple[bool, str | None]:
        return _match_bearer(header, self._allowed)


def make_dashboard_auth(*, secrets_obj: object | None, key_path: Path) -> DashboardAuth:
    """Build a :class:`DashboardAuth` from the gateway's secrets
    config. The ``secrets_obj`` shape is intentionally loose
    (``object | None``) so tests can pass mocks; production
    always passes a :class:`gateway.config.Secrets` instance.
    """
    bearers: list[tuple[str, str | None]] = []
    if secrets_obj is not None:
        tokens = getattr(secrets_obj, "allowed_tokens", None) or []
        for entry in tokens:
            bearers.append((entry.token, entry.client))
    return DashboardAuth(allowed_bearers=bearers, key_path=key_path)


# --------------------------------------------------------------- runtime auth


def authorize_request(request: Request) -> Response | None:
    """Run on every dashboard route's entry. Returns ``None``
    when the request is authorised (the route should proceed)
    or a :class:`Response` to short-circuit to (login redirect
    or 401).

    Authorisation, in order:

    1. ``Authorization: Bearer <token>`` matches an allowed
       token. Sets ``request.state.client`` and returns None.
    2. ``Cookie: fitt_dashboard=<signed>`` decodes successfully.
       Sets ``request.state.client`` and returns None.
    3. Bearer header present but invalid → 401 JSON. No
       redirect; a bad bearer is almost always a tool error.
    4. Otherwise → redirect to ``/dashboard/login?next=<path>``.
    """
    auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
    if auth is None:
        # The dashboard wasn't wired into this app. Treat this
        # as "no access" rather than "everything's fine" — the
        # safer default if a future refactor accidentally
        # mounts the router without the dependency.
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "dashboard_unconfigured",
                    "message": "dashboard auth context is not initialised",
                }
            },
        )

    bearer_header = request.headers.get("authorization")
    if bearer_header is not None:
        ok, tag = auth.match_bearer(bearer_header)
        if ok:
            request.state.client = tag or "webui"
            return None
        # Header present, doesn't match any token. Don't
        # silently fall through to the cookie path — that
        # would let a tool with a stale token mistakenly
        # use a cookie issued for someone else's session.
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "type": "auth_error",
                    "message": "Bearer token did not match any allowed token.",
                    "code": "unauthorized",
                }
            },
        )

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie is not None:
        payload = auth.verify_cookie(cookie)
        if payload is not None:
            request.state.client = payload.client
            return None

    # No bearer, no valid cookie. Send the operator to the
    # login form with a ``next`` parameter so they land back
    # where they tried to go.
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    redirect = RedirectResponse(
        url=f"/dashboard/login?next={next_path}",
        status_code=302,
    )
    # If a cookie was present but invalid, drop it. Browsers
    # will keep retrying with a stale cookie otherwise.
    if cookie is not None:
        redirect.delete_cookie(COOKIE_NAME, path="/")
    return redirect
