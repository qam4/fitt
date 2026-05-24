"""Tests for the dashboard's view layer (Phase 7 Slice 7.5
Tasks 23-27). Today covers the overview page, the placeholder
views for not-yet-shipped pages, and the static asset mount.

The overview view is the centerpiece of the foundation slice
because it exercises every aggregation hook the dashboard
will use later (alias listing, context windows, probe results,
MCP, cron, gaps, pruners, telegram). Subsequent slices add
specialised views without rebuilding this scaffolding.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.alias_probe import ProbeResult
from gateway.app import create_app
from gateway.context_window import ContextWindowResult

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    return TestClient(app, follow_redirects=False)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# --------------------------------------------------------------- overview


def test_overview_renders_with_minimal_state(client: TestClient) -> None:
    """The overview page must render on a fresh gateway with
    no probe data, no context cache, no MCP servers, no cron
    jobs — just the configured aliases."""
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Headline metrics rendered.
    assert "Uptime" in body
    assert "Aliases" in body
    assert "MCP servers" in body
    assert "Cron jobs" in body
    # Configured aliases each get a row.
    assert "fitt-default" in body
    assert "fitt-smart" in body
    assert "fitt-fast" in body


def test_overview_surfaces_context_window(client: TestClient) -> None:
    """Cached context window from Slice 7.1 surfaces in the
    overview's alias table."""
    cache = client.app.state.context_windows
    cache._results[("ollama", "qwen-big")] = ContextWindowResult(
        tokens=32_768,
        source="modelfile",
        detail="num_ctx 32768",
        discovered_at=1779479823.42,
    )
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    # The renderer formats 32_768 as "32k".
    assert "32k" in r.text


def test_overview_surfaces_probe_status(client: TestClient) -> None:
    """The 'pip' status indicator reflects the boot probe's
    last result for each alias."""
    client.app.state.alias_probe_results = {
        "fitt-default": ProbeResult(
            alias="fitt-default",
            status="ok",
            detail="emitted 1 tool call(s) as expected",
            model_used="qwen2.5-coder:14b",
        )
    }
    client.app.state.alias_probe_ran_at = 1779479823.42
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    assert "pip-ok" in r.text


def test_overview_partial_returns_panel_only(client: TestClient) -> None:
    """The HTMX-driven partial endpoint returns just the panel
    fragment, not a full page. Used by ``hx-get`` polling for
    in-place refresh."""
    r = client.get("/dashboard/_partials/overview", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # The fragment renders the metrics grid but not the
    # base.html chrome.
    assert "Uptime" in body
    assert '<aside class="sidebar">' not in body  # no nav


def test_overview_redirects_without_auth(client: TestClient) -> None:
    """No bearer / no cookie still redirects to login."""
    r = client.get("/dashboard/")
    assert r.status_code == 302


# --------------------------------------------------------------- placeholder views


@pytest.mark.parametrize(
    "path,nav,title_substring",
    [
        ("/dashboard/aliases", "aliases", "Aliases"),
        ("/dashboard/turns", "turns", "Turns"),
        ("/dashboard/turns/main", "turns", "Turns"),
        ("/dashboard/turns/main/abc", "turns", "Turns"),
        ("/dashboard/tools", "tools", "Tools"),
        ("/dashboard/cron", "cron", "Cron"),
        ("/dashboard/audit", "audit", "Audit"),
        ("/dashboard/health", "health", "Health"),
        ("/dashboard/gaps", "gaps", "Capability gaps"),
    ],
)
def test_placeholder_views_render(
    client: TestClient,
    path: str,
    nav: str,
    title_substring: str,
) -> None:
    """Every sidebar link lands on a working page during
    the foundation slice. Real views replace the placeholder
    in subsequent commits without changing the URL shape."""
    r = client.get(path, headers=_auth())
    assert r.status_code == 200, f"{path} returned {r.status_code}"
    assert title_substring in r.text
    # Sidebar lights up the right nav entry.
    assert "active" in r.text  # `class="active"` rendered somewhere


def test_placeholder_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/aliases")
    assert r.status_code == 302


# --------------------------------------------------------------- static


def test_static_css_served(client: TestClient) -> None:
    """The vendored stylesheet is reachable without auth (a
    missing CSS file would be a 404 every page would suffer)."""
    r = client.get("/dashboard/static/style.css")
    assert r.status_code == 200
    assert "FITT dashboard" in r.text  # comment header in style.css


def test_static_htmx_served(client: TestClient) -> None:
    """htmx.min.js is vendored locally — Tailscale-only
    deployments don't need internet access for the dashboard
    to work."""
    r = client.get("/dashboard/static/htmx.min.js")
    assert r.status_code == 200
    ct = r.headers["content-type"].lower()
    assert "javascript" in ct
    # The minified bundle starts with htmx's IIFE wrapper.
    assert "htmx" in r.text[:200].lower()


# --------------------------------------------------------------- nav highlight


def test_overview_nav_highlights_overview(client: TestClient) -> None:
    """The active nav class is on the Overview link, not on
    one of the others."""
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Cheap structural check: the Overview link is first in
    # the sidebar and carries the active class.
    overview_pos = body.index('href="/dashboard/"')
    aliases_pos = body.index('href="/dashboard/aliases"')
    assert overview_pos < aliases_pos
    # The active class must be on the overview link, not
    # the aliases link.
    overview_segment = body[overview_pos : overview_pos + 100]
    aliases_segment = body[aliases_pos : aliases_pos + 100]
    assert "active" in overview_segment
    assert "active" not in aliases_segment


# --------------------------------------------------------------- bearer in cookie context


def test_overview_works_with_session_cookie(client: TestClient) -> None:
    """After login via the form, the cookie alone is enough."""
    client.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )
    # No Authorization header — relying on the cookie jar.
    r = client.get("/dashboard/")
    assert r.status_code == 200
    assert "Uptime" in r.text
