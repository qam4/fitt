"""Tests for /health, /ready, /v1/models.

/ready probes use httpx, which we mock with respx. That lets us
simulate reachable, unreachable, and timed-out backends without real
network traffic.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.app import create_app

from ._fixtures import build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def test_health_200(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_models_lists_aliases(client: TestClient) -> None:
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    assert ids == {"fitt-default", "fitt-smart", "fitt-fast"}


def test_models_includes_fitt_extensions(client: TestClient) -> None:
    r = client.get("/v1/models")
    data = r.json()["data"]
    smart = next(m for m in data if m["id"] == "fitt-smart")
    assert smart["fitt_backend"] == "openrouter"


def _mock_all_reachable() -> respx.Router:
    router = respx.mock(assert_all_called=False)
    router.get("http://laptop.tailnet:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": []})
    )
    router.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": []})
    )
    router.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    return router


def test_ready_200_when_all_reachable(client: TestClient) -> None:
    with _mock_all_reachable():
        r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "failing" not in body


def test_ready_503_when_one_alias_unreachable(client: TestClient) -> None:
    router = respx.mock(assert_all_called=False)
    # Laptop Ollama and fallback both fail → fitt-default and fitt-fast
    # both unreachable.
    router.get("http://laptop.tailnet:11434/api/tags").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    router.get("http://localhost:11434/api/tags").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    router.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    with router:
        r = client.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert set(body["failing"]) == {"fitt-default", "fitt-fast"}
