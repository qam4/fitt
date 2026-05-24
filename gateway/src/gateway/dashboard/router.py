"""Dashboard router: mount point, login, logout, static.

Per-view routes (overview, aliases, turns, tools, cron,
audit, health, gaps) live in :mod:`gateway.dashboard.views`
and are included from here so the mount surface stays in
one place.

Auth model
----------

The dashboard prefix is **excluded** from the global
:class:`gateway.auth.AuthMiddleware`. Instead, every route
under ``/dashboard`` (except ``login`` and the static file
mount) calls :func:`gateway.dashboard.auth.authorize_request`
as its first action and short-circuits if the request fails
auth.

Why excluded from the middleware: the dashboard wants to
*redirect* to a login page when a browser hits a protected
route without credentials, not return a 401 JSON. Mixing
the two semantics in one middleware would be ugly. Keeping
auth as an inline check inside the dashboard router lets
the global middleware stay strict-401 for ``/v1/*``.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import COOKIE_NAME, COOKIE_TTL_SECONDS, DashboardAuth, authorize_request

_log = logging.getLogger(__name__)

DASHBOARD_PREFIX = "/dashboard"
"""Mount path. Centralised so the auth middleware exemption
list and the router's routes can't drift."""


_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FITT dashboard - login</title>
<style>
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #1a1a1a;
  color: #ddd;
  margin: 0;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
}
.card {
  background: #222;
  border: 1px solid #333;
  border-radius: 8px;
  padding: 32px;
  width: 360px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
h1 {
  margin: 0 0 8px;
  font-size: 20px;
  font-weight: 500;
}
p.tagline {
  margin: 0 0 24px;
  color: #888;
  font-size: 13px;
}
label {
  display: block;
  margin-bottom: 8px;
  font-size: 13px;
  color: #aaa;
}
input[type=password] {
  width: 100%;
  padding: 8px 12px;
  font-size: 14px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  background: #1a1a1a;
  border: 1px solid #444;
  color: #fff;
  border-radius: 4px;
  box-sizing: border-box;
}
button {
  width: 100%;
  margin-top: 16px;
  padding: 10px;
  background: #2a6;
  color: #fff;
  border: none;
  border-radius: 4px;
  font-size: 14px;
  cursor: pointer;
}
button:hover {
  background: #38c;
}
.error {
  background: #5a1a1a;
  border: 1px solid #a33;
  color: #fbb;
  padding: 8px 12px;
  border-radius: 4px;
  margin-bottom: 16px;
  font-size: 13px;
}
</style>
</head>
<body>
<div class="card">
<h1>FITT dashboard</h1>
<p class="tagline">Paste a bearer token to sign in.</p>
__ERROR__
<form method="post" action="/dashboard/login">
<input type="hidden" name="next" value="__NEXT__">
<label for="token">Bearer token</label>
<input type="password" name="token" id="token" autocomplete="current-password" autofocus>
<button type="submit">Sign in</button>
</form>
</div>
</body>
</html>
"""


def _render_login(*, error: str | None = None, next_path: str = "/dashboard") -> str:
    """Inline template for the login page.

    Why inline: the login page needs to render before any
    auth check, and the rest of the dashboard's templates
    live in a directory that gets wired up at app build time.
    Keeping the login HTML self-contained means a half-built
    app can still serve a working login page during the
    redirect-to-login fallback path.
    """
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    safe_next = html.escape(next_path, quote=True)
    return _LOGIN_HTML.replace("__ERROR__", error_html).replace("__NEXT__", safe_next)


def build_router() -> APIRouter:
    """Construct the dashboard's APIRouter.

    A factory rather than a module-level singleton so tests
    can mount the router into a fresh FastAPI app without
    state leaking between cases.
    """
    router = APIRouter(prefix=DASHBOARD_PREFIX)

    # ----------------------------------------------------------- login

    @router.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request, next: str = "/dashboard") -> HTMLResponse:
        """Render the login form. ``next`` is propagated so the
        operator lands back where they tried to go after
        signing in. We intentionally don't validate the
        ``next`` against an allowlist beyond the prefix check —
        a malicious operator with shell access doesn't need an
        open-redirect attack chain."""
        # Keep the redirect inside the dashboard surface to
        # blunt obvious open-redirect mistakes.
        target = next if next.startswith(DASHBOARD_PREFIX) else DASHBOARD_PREFIX
        return HTMLResponse(_render_login(next_path=target))

    @router.post("/login", response_class=HTMLResponse)
    async def login_post(
        request: Request,
        token: str = Form(""),
        next: str = Form("/dashboard"),
    ) -> Response:
        """Validate the pasted token and issue a session cookie.

        Only bearer-tagged tokens (the ``client:`` field on
        the matching ``allowed_tokens`` entry) get a non-trivial
        client identity in the cookie; untagged tokens default
        to ``webui`` (least-trust) the same way the global
        AuthMiddleware does for unmarked tokens."""
        auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
        if auth is None:
            return HTMLResponse(
                _render_login(
                    error="Dashboard auth is not configured on this gateway.",
                    next_path=next,
                ),
                status_code=503,
            )
        ok, tag = auth.match_bearer(f"Bearer {token.strip()}")
        if not ok:
            # Same posture as the chat endpoint's 401: don't
            # leak which bit was wrong (token vs missing).
            _log.info("dashboard.login_failed")
            return HTMLResponse(
                _render_login(
                    error="That token did not match. Try again.",
                    next_path=next,
                ),
                status_code=401,
            )

        client = tag or "webui"
        cookie_value = auth.issue_cookie(client=client)
        target = next if next.startswith(DASHBOARD_PREFIX) else DASHBOARD_PREFIX
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            cookie_value,
            max_age=COOKIE_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
            # No ``secure=True`` flag: FITT runs on a Tailscale
            # interface that may not present TLS. Operators
            # exposing the dashboard publicly should put a
            # reverse proxy in front and then layer their own
            # secure-cookie middleware, same as for the chat
            # endpoint. Documented in the operator README.
        )
        _log.info("dashboard.login_ok", extra={"client": client})
        return response

    @router.get("/logout")
    async def logout() -> Response:
        """Clear the session cookie and bounce to login."""
        response = RedirectResponse(url=f"{DASHBOARD_PREFIX}/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # ----------------------------------------------------------- placeholder root

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    async def root_no_slash(request: Request) -> Response:
        """Bounce ``/dashboard`` (no trailing slash) to the
        canonical ``/dashboard/`` so the auth check + view
        rendering only have one entry point to reason about."""
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return RedirectResponse(url=f"{DASHBOARD_PREFIX}/", status_code=307)

    @router.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> Response:
        """Overview page placeholder — a real implementation
        lands in the next slice-7.5 commit (Task 24). Today
        this just confirms the auth flow works end-to-end so
        the foundation can ship before the views do."""
        guard = authorize_request(request)
        if guard is not None:
            return guard
        client = getattr(request.state, "client", "webui")
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>FITT dashboard</title></head>
<body style="font-family: sans-serif; padding: 40px; color: #ddd; background: #1a1a1a;">
<h1>FITT dashboard</h1>
<p>Signed in as <code>{html.escape(client)}</code>.</p>
<p>Views land in the next commits of Slice 7.5. For now this
page just confirms the auth flow works.</p>
<p><a href="/dashboard/logout" style="color:#8ac;">Sign out</a></p>
</body></html>
"""
        )

    return router


def add_static_mount(app, *, static_dir: Path) -> None:  # type: ignore[no-untyped-def]
    """Mount the dashboard's static asset directory at
    ``/dashboard/static``. Optional — if the directory doesn't
    exist (a slice that hasn't shipped yet, a stripped-down
    Docker layer), the mount is skipped and the router still
    works without CSS / HTMX. This keeps the foundation slice
    runnable before the static-asset slice (Task 23) lands.
    """
    if not static_dir.exists():
        return
    from fastapi.staticfiles import StaticFiles

    app.mount(
        f"{DASHBOARD_PREFIX}/static",
        StaticFiles(directory=str(static_dir)),
        name="dashboard_static",
    )
