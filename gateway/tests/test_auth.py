"""Auth middleware tests.

Covers Phase 1 Property 2 (auth enforcement) and acceptance criteria
3.1, 3.3, 3.4.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app

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
