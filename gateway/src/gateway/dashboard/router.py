"""Dashboard router: mount point, login, logout, static, views.

Composes three concerns under the ``/dashboard`` prefix:

1. The login surface (``/login`` + ``/logout``) — entry points
   that don't require authentication.
2. The static asset mount (``/static``) — CSS and the
   vendored htmx.min.js.
3. The view sub-router from :mod:`gateway.dashboard.views` —
   every authenticated page (overview, aliases, turns, ...).

Auth model
----------

The dashboard prefix is **excluded** from the global
:class:`gateway.auth.AuthMiddleware` so that protected pages
can return 302-redirects to the login form when a browser
hits them without credentials. The view handlers and the root
route call :func:`gateway.dashboard.auth.authorize_request`
inline as their first action.

Login + static endpoints don't call ``authorize_request`` —
they're the entry path. Login is open by design (the operator
needs somewhere to type their token); static assets are
served by Starlette's ``StaticFiles`` mount which has no
auth concept.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import COOKIE_NAME, COOKIE_TTL_SECONDS, DashboardAuth
from .views import build_views_router, templates

_log = logging.getLogger(__name__)

DASHBOARD_PREFIX = "/dashboard"
"""Mount path. Centralised so the auth middleware exemption
list and the router's routes can't drift."""


def _safe_next(next_path: str) -> str:
    """Clamp ``next_path`` to the dashboard prefix.

    Operators using the dashboard via Tailscale don't need
    open-redirect protection in the formal sense, but the
    cost of clamping is one comparison and the clarity gain
    is real."""
    if next_path.startswith(DASHBOARD_PREFIX):
        return next_path
    return DASHBOARD_PREFIX


def build_router() -> APIRouter:
    """Construct the dashboard's APIRouter.

    A factory rather than a module-level singleton so tests
    can mount the router into a fresh FastAPI app without
    state leaking between cases.
    """
    router = APIRouter(prefix=DASHBOARD_PREFIX)

    # ----------------------------------------------------------- login

    @router.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request, next: str = "/dashboard/") -> Response:
        """Render the login form. ``next`` is propagated so the
        operator lands back where they tried to go after
        signing in."""
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next_path": _safe_next(next), "error": None},
        )

    @router.post("/login", response_class=HTMLResponse)
    async def login_post(
        request: Request,
        token: str = Form(""),
        next: str = Form("/dashboard/"),
    ) -> Response:
        """Validate the pasted token and issue a session cookie.

        Only bearer-tagged tokens (the ``client:`` field on
        the matching ``allowed_tokens`` entry) get a non-trivial
        client identity in the cookie; untagged tokens default
        to ``webui`` (least-trust) the same way the global
        AuthMiddleware does for unmarked tokens."""
        auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
        if auth is None:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next_path": _safe_next(next),
                    "error": "Dashboard auth is not configured on this gateway.",
                },
                status_code=503,
            )
        ok, tag = auth.match_bearer(f"Bearer {token.strip()}")
        if not ok:
            _log.info("dashboard.login_failed")
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next_path": _safe_next(next),
                    "error": "That token did not match. Try again.",
                },
                status_code=401,
            )

        client = tag or "webui"
        cookie_value = auth.issue_cookie(client=client)
        target = _safe_next(next)
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

    # ----------------------------------------------------------- entry redirect

    @router.get("", include_in_schema=False)
    async def root_no_slash() -> Response:
        """Bounce ``/dashboard`` to the canonical
        ``/dashboard/`` so the auth check + view rendering
        only have one entry point to reason about. The
        authentication check happens at ``/dashboard/`` —
        leaving this redirect open is fine because the only
        thing it discloses is the canonical URL.
        """
        return RedirectResponse(url=f"{DASHBOARD_PREFIX}/", status_code=307)

    # ----------------------------------------------------------- views

    # The views router contains every authenticated page. We
    # mount it *under* the dashboard prefix by including it
    # without an additional prefix — its own routes start at
    # ``/`` (which becomes ``/dashboard/`` once mounted).
    router.include_router(build_views_router())

    return router


def add_static_mount(app, *, static_dir: Path) -> None:  # type: ignore[no-untyped-def]
    """Mount the dashboard's static asset directory at
    ``/dashboard/static``. Optional — if the directory doesn't
    exist (a stripped-down Docker layer), the mount is skipped
    and the templates degrade gracefully (links to missing CSS
    / JS won't break the rest of the page).
    """
    if not static_dir.exists():
        return
    from fastapi.staticfiles import StaticFiles

    app.mount(
        f"{DASHBOARD_PREFIX}/static",
        StaticFiles(directory=str(static_dir)),
        name="dashboard_static",
    )
