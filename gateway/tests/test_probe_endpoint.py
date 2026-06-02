"""Tests for ``POST /v1/probe/<alias>`` — Phase 7.6 Decision 4.

Three concerns:

* Shape: the endpoint returns the documented ProbeResult fields
  (alias, status, detail, latency_ms, model_used, finish_reason,
  reply_preview, reachable).
* Auth + 404: bearer-gated; unknown alias returns 404 with a
  clean error envelope (mirrors the eval endpoint).
* Side effect: the run updates ``app.state.alias_probe_results``
  so the dashboard's last-probe reflects the fresh run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gateway.alias_probe import ProbeResult
from gateway.app import create_app

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# --------------------------------------------------------------- auth


def test_probe_requires_bearer(client: TestClient) -> None:
    r = client.post("/v1/probe/fitt-default")
    assert r.status_code == 401


# --------------------------------------------------------------- 404


def test_probe_unknown_alias_returns_404(client: TestClient) -> None:
    r = client.post("/v1/probe/nonexistent", headers=_auth())
    assert r.status_code == 404
    detail = r.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["error"]["type"] == "unknown_alias"
    assert "available" in detail["error"]


# --------------------------------------------------------------- success shape


def test_probe_returns_summary_shape(client: TestClient) -> None:
    """Patch ``probe_alias`` so the test runs in milliseconds.
    The endpoint's job is wrapping the probe; the probe's own
    behavior is covered in test_alias_probe.py."""
    result = ProbeResult(
        alias="fitt-default",
        status="ok",
        detail="emitted 1 tool call(s) as expected",
        latency_ms=1234,
        model_used="qwen2.5-coder:14b",
        finish_reason="tool_calls",
    )

    async def _stub(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return result

    with patch("gateway.probe_endpoint.probe_alias", side_effect=_stub):
        r = client.post("/v1/probe/fitt-default", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["alias"] == "fitt-default"
    assert body["status"] == "ok"
    assert body["latency_ms"] == 1234
    assert body["model_used"] == "qwen2.5-coder:14b"
    assert body["finish_reason"] == "tool_calls"
    assert body["reachable"] is None


def test_probe_carries_nonok_status_with_200(client: TestClient) -> None:
    """A dispatch failure is a classified status in the body, not
    an HTTP error: the probe never raises, so the HTTP call is a
    clean 200 carrying e.g. ``upstream_silent``."""
    result = ProbeResult(
        alias="fitt-default",
        status="upstream_silent",
        detail="probe timed out after 10s but endpoint is reachable (42ms ping)",
        latency_ms=10000,
        model_used="qwen2.5-coder:14b",
        reachable=True,
    )

    async def _stub(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return result

    with patch("gateway.probe_endpoint.probe_alias", side_effect=_stub):
        r = client.post("/v1/probe/fitt-default", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "upstream_silent"
    assert body["reachable"] is True


# --------------------------------------------------------------- side effect


def test_probe_updates_app_state(tmp_path: Path) -> None:
    """The on-demand probe stashes its result on
    ``app.state.alias_probe_results`` so the dashboard's
    last-probe view reflects the fresh run."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app)

    result = ProbeResult(
        alias="fitt-default",
        status="narrated",
        detail="model replied with text instead of tool_calls",
        latency_ms=900,
        model_used="qwen2.5-coder:14b",
        finish_reason="stop",
        reply_preview="Sure, I'll do that...",
    )

    async def _stub(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return result

    with patch("gateway.probe_endpoint.probe_alias", side_effect=_stub):
        r = tc.post("/v1/probe/fitt-default", headers=_auth())
    assert r.status_code == 200, r.text

    stored = app.state.alias_probe_results.get("fitt-default")
    assert stored is not None
    assert stored.status == "narrated"
    assert app.state.alias_probe_ran_at is not None
