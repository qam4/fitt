"""Auth middleware tests.

Covers Phase 1 Property 2 (auth enforcement) and acceptance criteria
3.1, 3.3, 3.4.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import AllowedToken, Secrets

from ._fixtures import PERSONAL_TOKEN, WRONG_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


# ---------- exempt endpoints (no token required) -----------------


def test_auth_skips_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200


def test_auth_skips_models_endpoint(client: TestClient) -> None:
    r = client.get("/v1/models")
    assert r.status_code == 200


# ---------- gated endpoints ---------------------------------------

# Use /v1/models-nope (a non-existent /v1/* path) to assert that auth
# runs *before* routing. A 401 from auth should beat a 404 from the
# router.


def test_auth_rejects_missing_token_on_v1(client: TestClient) -> None:
    r = client.post("/v1/chat/completions", json={})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["type"] == "auth_error"


def test_auth_rejects_wrong_token_on_v1(client: TestClient) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={},
        headers={"Authorization": f"Bearer {WRONG_TOKEN}"},
    )
    assert r.status_code == 401


def test_auth_rejects_malformed_header(client: TestClient) -> None:
    # Not "Bearer <token>"
    r = client.post(
        "/v1/chat/completions",
        json={},
        headers={"Authorization": PERSONAL_TOKEN},  # missing "Bearer "
    )
    assert r.status_code == 401


def test_auth_rejects_empty_bearer(client: TestClient) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


def test_auth_accepts_valid_token_and_passes_through(client: TestClient) -> None:
    """Valid token means auth lets the request through.

    The chat endpoint itself may 400/422 on an empty body, but critically
    it should NOT be 401. That's what "auth accepts" means at this layer.
    """
    r = client.post(
        "/v1/chat/completions",
        json={},
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    )
    assert r.status_code != 401


# ---------- client tag propagation --------------------------------


def test_auth_sets_client_state_for_tagged_token(tmp_path: Path) -> None:
    """Tagged token → ``request.state.client`` reflects the tag.

    Downstream handlers (approval routing, per-client tool policy,
    audit logging) read ``request.state.client`` to know *which*
    client is calling. We verify it end-to-end by mounting a tiny
    probe route inside the auth-protected /v1 space.
    """
    cfg = build_test_config(tmp_path)
    # Replace the default personal token with an `ide`-tagged one
    # so we can assert the tag is propagated, not just present.
    assert cfg.secrets is not None
    cfg.secrets = Secrets(
        allowed_tokens=[
            AllowedToken(name="ide-token", token=PERSONAL_TOKEN, client="ide"),
        ],
        openrouter_api_key=cfg.secrets.openrouter_api_key,
    )
    app = create_app(cfg)

    router = APIRouter()

    @router.get("/v1/_probe_client")
    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({"client": request.state.client})

    app.include_router(router)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"client": "ide"}


def test_auth_sets_client_state_to_webui_for_untagged_token(tmp_path: Path) -> None:
    """Untagged token → ``request.state.client == 'webui'`` (safe default)."""
    cfg = build_test_config(tmp_path)
    # The default fixture already uses an untagged token; be explicit.
    assert cfg.secrets is not None
    assert cfg.secrets.allowed_tokens[0].client is None
    app = create_app(cfg)

    router = APIRouter()

    @router.get("/v1/_probe_client")
    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({"client": request.state.client})

    app.include_router(router)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"client": "webui"}


# ---------- X-FITT-Client header identification -------------------
# The header exists so well-behaved clients (the Telegram bot
# especially) can self-identify without the operator having to tag
# the token in secrets.yaml. See module docstring in auth.py.


def _mount_probe(app) -> None:  # type: ignore[no-untyped-def]
    """Helper: add a tiny auth-protected GET that echoes the
    resolved client tag. Kept private to this test module."""
    router = APIRouter()

    @router.get("/v1/_probe_client")
    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({"client": request.state.client})

    app.include_router(router)


def test_auth_header_overrides_untagged_token(tmp_path: Path) -> None:
    """Untagged token + `X-FITT-Client: telegram` → telegram.

    This is the primary use case: the bot sends the header so its
    requests are tagged correctly even if the operator forgot to
    add `client: telegram` to secrets.yaml."""
    cfg = build_test_config(tmp_path)
    assert cfg.secrets is not None
    assert cfg.secrets.allowed_tokens[0].client is None
    app = create_app(cfg)
    _mount_probe(app)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Client": "telegram",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"client": "telegram"}


def test_auth_header_agrees_with_token_tag(tmp_path: Path) -> None:
    """Tagged token + matching header → same tag, no error."""
    cfg = build_test_config(tmp_path)
    assert cfg.secrets is not None
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="bot", token=PERSONAL_TOKEN, client="telegram")],
        openrouter_api_key=cfg.secrets.openrouter_api_key,
    )
    app = create_app(cfg)
    _mount_probe(app)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Client": "telegram",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"client": "telegram"}


def test_auth_header_disagrees_with_token_tag_rejects(tmp_path: Path) -> None:
    """Tagged token + different header → 400, fail loud.

    We don't silently favour one over the other because a silent
    win could mask an operator mistake. A 400 with both values
    shows operators exactly what's wrong."""
    cfg = build_test_config(tmp_path)
    assert cfg.secrets is not None
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="ide-token", token=PERSONAL_TOKEN, client="ide")],
        openrouter_api_key=cfg.secrets.openrouter_api_key,
    )
    app = create_app(cfg)
    _mount_probe(app)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Client": "telegram",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "client_mismatch"
    # Both values should appear so operators know what to fix.
    assert "telegram" in body["error"]["message"]
    assert "ide" in body["error"]["message"]


def test_auth_header_with_invalid_value_rejects(tmp_path: Path) -> None:
    """Header present but not one of ide/telegram/webui/cli → 400."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    _mount_probe(app)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Client": "not-a-real-client",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "client_mismatch"


def test_auth_header_case_insensitive(tmp_path: Path) -> None:
    """Header values are normalised to lower-case before matching.

    Telegram's own User-Agent style has sometimes encouraged
    Pascal-Case; we accept ``Telegram`` and ``TELEGRAM`` the same
    as ``telegram``."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    _mount_probe(app)

    c = TestClient(app)
    r = c.get(
        "/v1/_probe_client",
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Client": "Telegram",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"client": "telegram"}
