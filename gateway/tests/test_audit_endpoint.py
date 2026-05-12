"""Tests for Phase 4.8c ``GET /v1/audit``.

Paged read over ``audit.jsonl`` with the ``{entries, next_since}``
cursor shape. No HMAC verification here — that stays a CLI concern
so a polling subscriber can't DoS the gateway.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.audit import new_entry

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _seed(client: TestClient, *, rows: list[tuple[str, float]]) -> None:
    """Append audit entries. ``rows`` is ``[(tool, ts), ...]``."""
    audit = client.app.state.audit
    for tool, ts in rows:
        audit.append(
            new_entry(
                session_key="main",
                client="telegram",
                tool=tool,
                args={},
                decision="auto",
                ok=True,
                ts=ts,
            )
        )


def test_audit_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/audit")
    assert r.status_code == 401


def test_audit_empty_when_log_fresh(client: TestClient) -> None:
    r = client.get("/v1/audit", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"entries": [], "next_since": None}


def test_audit_returns_entries(client: TestClient) -> None:
    _seed(
        client,
        rows=[("read_file", 100.0), ("git_status", 200.0)],
    )
    r = client.get("/v1/audit", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"entries", "next_since"}
    tools = [e["tool"] for e in body["entries"]]
    assert tools == ["read_file", "git_status"]
    # Each entry should carry the HMAC-chained fields so a
    # consumer can verify independently if it wants.
    assert "hmac" in body["entries"][0]
    assert "prev_hmac" in body["entries"][0]


def test_audit_since_exclusive(client: TestClient) -> None:
    _seed(
        client,
        rows=[("a", 100.0), ("b", 200.0), ("c", 300.0)],
    )
    r = client.get("/v1/audit?since=200", headers=_auth())
    tools = [e["tool"] for e in r.json()["entries"]]
    assert tools == ["c"]


def test_audit_tool_filter(client: TestClient) -> None:
    _seed(
        client,
        rows=[
            ("read_file", 100.0),
            ("git_status", 200.0),
            ("read_file", 300.0),
        ],
    )
    r = client.get("/v1/audit?tool=read_file", headers=_auth())
    tss = [e["ts"] for e in r.json()["entries"]]
    assert tss == [100.0, 300.0]


def test_audit_limit_keeps_most_recent(client: TestClient) -> None:
    _seed(client, rows=[(f"t_{i}", float(i)) for i in range(10)])
    r = client.get("/v1/audit?limit=3", headers=_auth())
    body = r.json()
    tools = [e["tool"] for e in body["entries"]]
    assert tools == ["t_7", "t_8", "t_9"]
    assert body["next_since"] == 9.0


def test_audit_next_since_null_at_tail(client: TestClient) -> None:
    _seed(client, rows=[("a", 1.0), ("b", 2.0)])
    r = client.get("/v1/audit", headers=_auth())
    assert r.json()["next_since"] is None


def test_audit_limit_validates(client: TestClient) -> None:
    r = client.get("/v1/audit?limit=0", headers=_auth())
    assert r.status_code == 400
    r = client.get("/v1/audit?limit=10000", headers=_auth())
    assert r.status_code == 400
