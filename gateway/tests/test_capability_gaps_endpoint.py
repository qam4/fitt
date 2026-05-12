"""Tests for Phase 4.8c ``GET /v1/capability-gaps``.

Two modes:

* Default paged feed — ``{entries, next_since}``.
* ``ranked=true`` — grouped-by-action roll-up the operator
  reads to decide "what tool do I build next."
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.capabilities import GapReport

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _seed(client: TestClient, gaps: list[GapReport]) -> None:
    log = client.app.state.capability_gaps
    for g in gaps:
        log.append(g)


def test_capability_gaps_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/capability-gaps")
    assert r.status_code == 401


def test_capability_gaps_empty(client: TestClient) -> None:
    r = client.get("/v1/capability-gaps", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"entries": [], "next_since": None}


def test_capability_gaps_paged_feed(client: TestClient) -> None:
    _seed(
        client,
        [
            GapReport(ts=100.0, session_key="main", action="fetch a webpage", suggestion=""),
            GapReport(
                ts=200.0,
                session_key="main",
                action="query postgres",
                suggestion="pg_query",
            ),
        ],
    )
    r = client.get("/v1/capability-gaps", headers=_auth())
    body = r.json()
    actions = [e["action"] for e in body["entries"]]
    assert actions == ["fetch a webpage", "query postgres"]
    assert body["next_since"] is None


def test_capability_gaps_since_exclusive(client: TestClient) -> None:
    _seed(
        client,
        [
            GapReport(ts=100.0, session_key="main", action="a", suggestion=""),
            GapReport(ts=200.0, session_key="main", action="b", suggestion=""),
        ],
    )
    r = client.get("/v1/capability-gaps?since=100", headers=_auth())
    actions = [e["action"] for e in r.json()["entries"]]
    assert actions == ["b"]


def test_capability_gaps_ranked_mode(client: TestClient) -> None:
    """Ranked mode groups by canonical action, counts, surfaces
    most-recent ``last_ts`` + ``last_suggestion`` per group."""
    _seed(
        client,
        [
            GapReport(
                ts=100.0, session_key="main", action="fetch a webpage", suggestion="http_get"
            ),
            GapReport(
                ts=200.0, session_key="main", action="Fetch a webpage", suggestion="fetch_url"
            ),
            GapReport(ts=300.0, session_key="main", action="query postgres", suggestion=""),
        ],
    )
    r = client.get("/v1/capability-gaps?ranked=true", headers=_auth())
    body = r.json()
    assert set(body.keys()) == {"ranked"}
    rows = body["ranked"]
    # Canonicalised grouping: two fetch-webpage gaps, one postgres.
    assert rows[0]["action"] == "fetch a webpage"
    assert rows[0]["count"] == 2
    assert rows[0]["last_ts"] == 200.0
    assert rows[0]["last_suggestion"] == "fetch_url"
    assert rows[1]["action"] == "query postgres"
    assert rows[1]["count"] == 1


def test_capability_gaps_limit_validates(client: TestClient) -> None:
    r = client.get("/v1/capability-gaps?limit=0", headers=_auth())
    assert r.status_code == 400
    r = client.get("/v1/capability-gaps?limit=10000", headers=_auth())
    assert r.status_code == 400
