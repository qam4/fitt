"""Tests for ``/v1/events`` after the Phase 4.8c shape change.

Covers:
- Auth (401 without a Bearer token).
- Response shape ``{entries, next_since}``.
- ``since`` is exclusive (the cursor a poller just saw doesn't
  replay).
- ``kind`` filters by event kind.
- ``limit`` honours default + validation bounds.
- ``next_since`` is null at the tail and the newest ts otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.events import new_entry

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _seed(client: TestClient, *, kinds_and_ts: list[tuple[str, float]]) -> None:
    events = client.app.state.events
    for kind, ts in kinds_and_ts:
        events.append(
            new_entry(
                kind=kind,
                session_key="main",
                title=f"stub {kind}",
                body="stub body",
                meta={"from": "test"},
                ts=ts,
            )
        )


# --------------------------------------------------------------- auth


def test_events_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/events")
    assert r.status_code == 401


# --------------------------------------------------------------- empty


def test_events_empty_when_log_fresh(client: TestClient) -> None:
    r = client.get("/v1/events", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"entries": [], "next_since": None}


# --------------------------------------------------------------- shape


def test_events_returns_expected_shape(client: TestClient) -> None:
    _seed(client, kinds_and_ts=[("cron_fired", 100.0)])
    r = client.get("/v1/events", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"entries", "next_since"}
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    # Exact fields the bot's formatter reads.
    assert set(entry.keys()) == {"ts", "kind", "session_key", "title", "body", "meta"}
    assert entry["ts"] == 100.0
    assert entry["kind"] == "cron_fired"
    assert entry["session_key"] == "main"
    assert entry["meta"] == {"from": "test"}
    # Only one entry, below the default limit of 100, so the
    # caller has reached the tail.
    assert body["next_since"] is None


# --------------------------------------------------------------- filters


def test_events_since_is_exclusive(client: TestClient) -> None:
    """A poller passing back the ts it just saw should NOT
    receive that entry again. Matches the documented
    exclusive-cursor semantic."""
    _seed(
        client,
        kinds_and_ts=[
            ("cron_fired", 100.0),
            ("cron_completed", 200.0),
            ("agent_message", 300.0),
        ],
    )
    r = client.get("/v1/events?since=200", headers=_auth())
    kinds = [e["kind"] for e in r.json()["entries"]]
    assert kinds == ["agent_message"]


def test_events_kind_filter(client: TestClient) -> None:
    _seed(
        client,
        kinds_and_ts=[
            ("cron_fired", 100.0),
            ("cron_completed", 200.0),
            ("cron_fired", 300.0),
        ],
    )
    r = client.get("/v1/events?kind=cron_fired", headers=_auth())
    tss = [e["ts"] for e in r.json()["entries"]]
    assert tss == [100.0, 300.0]


def test_events_since_and_kind_combined(client: TestClient) -> None:
    _seed(
        client,
        kinds_and_ts=[
            ("cron_fired", 100.0),
            ("cron_completed", 200.0),
            ("cron_fired", 300.0),
        ],
    )
    r = client.get("/v1/events?since=150&kind=cron_fired", headers=_auth())
    tss = [e["ts"] for e in r.json()["entries"]]
    assert tss == [300.0]


# --------------------------------------------------------------- limit


def test_events_limit_keeps_most_recent(client: TestClient) -> None:
    _seed(
        client,
        kinds_and_ts=[(f"kind_{i}", float(i)) for i in range(10)],
    )
    r = client.get("/v1/events?limit=3", headers=_auth())
    body = r.json()
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds == ["kind_7", "kind_8", "kind_9"]
    # Filled the limit, so next_since is the last-seen ts.
    assert body["next_since"] == 9.0


def test_events_next_since_null_at_tail(client: TestClient) -> None:
    """When the response contains fewer than ``limit`` entries,
    the caller has reached the tail and ``next_since`` is
    null."""
    _seed(client, kinds_and_ts=[("k", 1.0), ("k", 2.0)])
    r = client.get("/v1/events?limit=100", headers=_auth())
    body = r.json()
    assert len(body["entries"]) == 2
    assert body["next_since"] is None


def test_events_limit_rejects_zero(client: TestClient) -> None:
    r = client.get("/v1/events?limit=0", headers=_auth())
    assert r.status_code == 400


def test_events_limit_rejects_huge(client: TestClient) -> None:
    r = client.get("/v1/events?limit=10000", headers=_auth())
    assert r.status_code == 400
