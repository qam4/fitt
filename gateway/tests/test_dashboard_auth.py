"""Tests for the dashboard's cookie-or-bearer auth foundation
(Phase 7 Slice 7.5 Task 22).

Five concerns:

* **Cookie codec** — sign / verify roundtrips, signature
  mismatch, expired cookies, malformed cookies.
* **Bearer path** — Authorization header with a valid token
  works, invalid token returns 401 (not redirect).
* **Cookie path** — login form issues a cookie, the cookie is
  accepted on protected pages, expired/missing cookies bounce
  to login.
* **Login flow** — GET renders the form, POST validates,
  redirects with the cookie.
* **Logout** — clears the cookie and bounces.

The dashboard's redirect-to-login behaviour is part of the
public contract (the operator opens the dashboard URL, gets
the form), so the test fixture mounts the real app rather
than just the router. That keeps the test honest about the
``/dashboard`` exemption in :class:`gateway.auth.AuthMiddleware`.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import fitt_home as _fitt_home
from gateway.dashboard.auth import (
    COOKIE_NAME,
    CookiePayload,
    decode_cookie,
    default_key_path,
    encode_cookie,
    load_or_generate_key,
)

from ._fixtures import PERSONAL_TOKEN, build_test_config

# --------------------------------------------------------------- fixtures


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    # Don't follow redirects automatically — half the tests
    # check the redirect target / status code.
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------- key file


def test_key_generated_on_first_use(tmp_path: Path) -> None:
    """First load creates the key file with non-empty bytes."""
    p = tmp_path / "dashboard.key"
    assert not p.exists()
    key = load_or_generate_key(p)
    assert p.exists()
    assert len(key) == 32
    assert key == p.read_bytes()


def test_key_round_trips_across_loads(tmp_path: Path) -> None:
    """Second load returns the same bytes — we don't rotate."""
    p = tmp_path / "dashboard.key"
    first = load_or_generate_key(p)
    second = load_or_generate_key(p)
    assert first == second


def test_default_key_path_under_fitt_home(tmp_path: Path) -> None:
    """The conventional path lives directly under FITT_HOME,
    next to ``audit.key``."""
    expected = tmp_path / "dashboard.key"
    assert default_key_path(tmp_path) == expected


# --------------------------------------------------------------- codec


def test_cookie_round_trip() -> None:
    key = b"\x42" * 32
    payload = CookiePayload(expires_at=time.time() + 3600, client="telegram")
    encoded = encode_cookie(payload, key=key)
    decoded = decode_cookie(encoded, key=key)
    assert decoded is not None
    assert decoded.client == "telegram"
    # Float roundtrip via int truncation in encoding.
    assert int(decoded.expires_at) == int(payload.expires_at)


def test_cookie_signature_mismatch_rejected() -> None:
    key_a = b"\x01" * 32
    key_b = b"\x02" * 32
    payload = CookiePayload(expires_at=time.time() + 3600, client="ide")
    encoded = encode_cookie(payload, key=key_a)
    assert decode_cookie(encoded, key=key_b) is None


def test_cookie_expired_rejected() -> None:
    key = b"\x42" * 32
    payload = CookiePayload(expires_at=time.time() - 1, client="cli")
    encoded = encode_cookie(payload, key=key)
    assert decode_cookie(encoded, key=key) is None


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "no-dot-no-signature",
        "a.b.c.d",  # ok-ish but neither half decodes
        "!!!.@@@",
        "validlooking.butwronglength",
    ],
)
def test_cookie_malformed_rejected(garbage: str) -> None:
    key = b"\x42" * 32
    assert decode_cookie(garbage, key=key) is None


# --------------------------------------------------------------- bearer path


def test_bearer_valid_returns_root(client: TestClient) -> None:
    """A valid bearer token in Authorization header bypasses
    the cookie path entirely. Same convenience as for /v1/*."""
    r = client.get(
        "/dashboard/",
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    )
    assert r.status_code == 200
    assert "FITT dashboard" in r.text


def test_bearer_invalid_returns_401(client: TestClient) -> None:
    """A bad bearer is a tool error, not a missing-login.
    Returns JSON 401, not a 302."""
    r = client.get(
        "/dashboard/",
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["type"] == "auth_error"


# --------------------------------------------------------------- redirect


def test_no_credentials_redirects_to_login(client: TestClient) -> None:
    """Browsers hitting a protected route without credentials
    get a 302 to the login form with a ``next`` parameter."""
    r = client.get("/dashboard/")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/dashboard/login?next=")


def test_invalid_cookie_redirects_and_drops_cookie(client: TestClient) -> None:
    """A stale cookie shouldn't keep the browser stuck in a
    failure loop. The redirect carries a Set-Cookie that
    deletes the bad cookie."""
    r = client.get(
        "/dashboard/",
        cookies={COOKIE_NAME: "garbage.signature"},
    )
    assert r.status_code == 302
    assert any(
        # TestClient lowercases headers; the cookie is dropped
        # via Max-Age=0 or Expires in the past.
        "fitt_dashboard" in raw.lower()
        and ("max-age=0" in raw.lower() or "expires=" in raw.lower())
        for raw in r.headers.get_list("set-cookie")
    )


# --------------------------------------------------------------- login


def test_login_get_renders_form(client: TestClient) -> None:
    r = client.get("/dashboard/login")
    assert r.status_code == 200
    assert 'name="token"' in r.text
    assert "FITT dashboard" in r.text


def test_login_post_with_valid_token_issues_cookie(client: TestClient) -> None:
    r = client.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/"
    # A cookie was issued.
    cookies = r.headers.get_list("set-cookie")
    assert any(COOKIE_NAME in raw for raw in cookies)
    assert any("httponly" in raw.lower() for raw in cookies)


def test_login_post_with_bad_token_returns_401(client: TestClient) -> None:
    r = client.post(
        "/dashboard/login",
        data={"token": "wrong", "next": "/dashboard/"},
    )
    assert r.status_code == 401
    assert "did not match" in r.text


def test_login_then_protected_page(client: TestClient) -> None:
    """Full happy-path flow: log in, follow the redirect, get
    the protected page."""
    login = client.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )
    assert login.status_code == 303

    # The TestClient's cookie jar carries the cookie forward.
    page = client.get("/dashboard/")
    assert page.status_code == 200
    assert "Signed in as" in page.text


def test_login_next_is_dashboard_only(client: TestClient) -> None:
    """``next`` outside the dashboard prefix is ignored — we
    don't trust user-supplied redirects to leave the surface."""
    r = client.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "https://attacker.example/"},
    )
    assert r.status_code == 303
    # Bounces to /dashboard, not the attacker URL.
    assert r.headers["location"] == "/dashboard"


# --------------------------------------------------------------- logout


def test_logout_clears_cookie(client: TestClient) -> None:
    # Sign in first.
    client.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )

    r = client.get("/dashboard/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"

    # The cookie is dropped.
    cookies = r.headers.get_list("set-cookie")
    assert any(COOKIE_NAME in raw for raw in cookies)


# --------------------------------------------------------------- middleware exemption


def test_v1_routes_still_require_bearer(client: TestClient) -> None:
    """The dashboard exemption from the global AuthMiddleware
    must NOT leak to ``/v1/*`` — those routes still need a
    bearer token."""
    r = client.get("/v1/aliases")
    assert r.status_code == 401


def test_dashboard_login_does_not_require_bearer(client: TestClient) -> None:
    """The login form is the entry point — it has to be
    reachable without credentials."""
    r = client.get("/dashboard/login")
    assert r.status_code == 200
    r = client.post(
        "/dashboard/login",
        data={"token": "anything"},
    )
    # Even bogus credentials get a styled response, not a
    # global 401 from the middleware.
    assert r.status_code == 401
    assert "<html" in r.text.lower()


# --------------------------------------------------------------- integration with FITT_HOME


def test_dashboard_key_lands_under_fitt_home(tmp_path: Path) -> None:
    """create_app constructs DashboardAuth with the canonical
    key path; the file gets created on first use under the
    tests' isolated FITT_HOME."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    # Trigger key generation by hitting the login flow.
    r = tc.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )
    assert r.status_code == 303

    expected = _fitt_home() / "dashboard.key"
    assert expected.exists()
    assert len(expected.read_bytes()) == 32
