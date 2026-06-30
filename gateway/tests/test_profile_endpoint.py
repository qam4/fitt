"""Tests for ``POST /v1/profile/<alias>`` — Phase 12.5a.

Concerns:

* Auth + 404: bearer-gated; unknown alias returns a clean 404
  envelope (mirrors the eval / probe endpoints).
* Shape: returns the profile JSON (declared + measured + markdown).
* Side effect: writes ``<alias>-profile.json`` under the gateway's
  ``$FITT_HOME/eval/`` — the dir the dashboard reads (the fix for the
  host-vs-container "No capability profile on disk" mismatch).

The producer (`run_profile`) is patched so the test runs in
milliseconds; its own behavior is covered in test_profile_runner.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.capability_profile import CapabilityProfile, DeclaredFact, MeasuredGrade

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _fake_profile() -> CapabilityProfile:
    return CapabilityProfile(
        alias="fitt-default",
        model_id="qwen-big",
        captured_at=datetime.now(UTC),
        declared=[DeclaredFact("tools", "true")],
        measured=[
            MeasuredGrade(name="plan-election", pass_rate=1.0, passes=3, valid=3, samples=3),
        ],
        resource=None,
    )


def test_profile_requires_bearer(client: TestClient) -> None:
    r = client.post("/v1/profile/fitt-default")
    assert r.status_code == 401


def test_profile_unknown_alias_returns_404(client: TestClient) -> None:
    r = client.post("/v1/profile/nonexistent", headers=_auth())
    assert r.status_code == 404
    detail = r.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["error"]["type"] == "unknown_alias"
    assert "available" in detail["error"]


def test_profile_returns_json_and_writes_under_fitt_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint writes via fitt_home(); pin it at tmp_path so the
    # write lands where we assert (and not in the real ~/.fitt).
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app)

    async def _stub(**_kwargs: Any) -> CapabilityProfile:
        return _fake_profile()

    with patch("gateway.profile_endpoint.run_profile", side_effect=_stub):
        r = tc.post("/v1/profile/fitt-default", headers=_auth())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["alias"] == "fitt-default"
    assert body["model_id"] == "qwen-big"
    assert any(g["name"] == "plan-election" for g in body["measured"])
    assert "markdown" in body

    # Written under the gateway's FITT_HOME/eval — the dir the dashboard reads.
    assert (tmp_path / "eval" / "fitt-default-profile.json").exists()
