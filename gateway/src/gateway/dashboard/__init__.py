"""Phase 7 Slice 7.5 — operator dashboard.

A small read-only web UI mounted at ``/dashboard`` on the
existing gateway. HTMX + server-rendered Jinja templates;
no SPA build step. The same data Telegram surfaces (model
in use, recent turns, system status, alias state, audit
tail), accessible from a desk browser when a longer
debugging session warrants it.

Six core views (each on its own route — see the spec under
``.kiro/specs/phase7-visibility-traceability/design.md``):

* ``/dashboard`` — overview ("is FITT okay right now?")
* ``/dashboard/aliases`` — per-binding state, context window,
  last probe, last eval
* ``/dashboard/turns/<session>`` — per-session turn browser
  (the centerpiece for traceability)
* ``/dashboard/tools`` — registered tools + last invocations
* ``/dashboard/cron`` — scheduled jobs (read-only in v0)
* ``/dashboard/audit`` — paged audit log tail with filters
* ``/dashboard/health`` — system status (mirrors ``/v1/status``)
* ``/dashboard/gaps`` — capability-gap log, ranked

Plus the auth surface:

* ``/dashboard/login`` — accepts a bearer token, sets a signed
  session cookie, redirects to the requested page
* ``/dashboard/logout`` — clears the cookie

Auth posture
------------

Two parallel paths:

1. ``Authorization: Bearer <token>`` works the same as for
   ``/v1/*`` — convenient for ``curl`` and for tools like
   Raycast widgets that hit dashboard JSON endpoints.
2. A signed session cookie issued by the login form, valid
   for 24h, signed with a key at ``$FITT_HOME/dashboard.key``
   (0600, generated on first use, same posture as
   ``audit.key``).

The dashboard is mounted under ``/dashboard`` and excluded
from the global :class:`gateway.auth.AuthMiddleware` so it
can do its own redirect-to-login flow rather than returning
401 JSON envelopes.

Tailscale-only by default — same posture as the chat
endpoint. Operators on a public-internet exposure are
responsible for their own reverse proxy.
"""

from __future__ import annotations

from .router import DASHBOARD_PREFIX, build_router

__all__ = ["DASHBOARD_PREFIX", "build_router"]
