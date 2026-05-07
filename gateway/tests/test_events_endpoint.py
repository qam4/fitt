"""Tests for ``/v1/events``.

Covers:
- Auth (401 without a Bearer token).
- ``since`` filters entries strictly later.
- ``kind`` filters by event kind.
- ``limit`` honours default + validation bounds.
- Response shape matches what ``EventLog.read`` produces.
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
    """Write the given events to the gateway's EventLog."""
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
    assert r.json() == {"events": []}


# --------------------------------------------------------------- shape


def test_events_returns_expected_shape(client: TestClient) -> None:
    _seed(client, kinds_and_ts=[("cron_fired", 100.0)])
    r = client.get("/v1/events", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "events" in body and len(body["events"]) == 1
    entry = body["events"][0]
    # Exact fields the bot's formatter needs — changing any of
    # these is a wire-compat break the pusher won't handle
    # gracefully.
    assert set(entry.keys()) == {"ts", "kind", "session_key", "title", "body", "meta"}
    assert entry["ts"] == 100.0
    assert entry["kind"] == "cron_fired"
    assert entry["session_key"] == "main"
    assert entry["meta"] == {"from": "test"}


# --------------------------------------------------------------- filters


def test_events_since_filters_older(client: TestClient) -> None:
    _seed(
        client,
        kinds_and_ts=[
            ("cron_fired", 100.0),
            ("cron_completed", 200.0),
            ("agent_message", 300.0),
        ],
    )
    r = client.get("/v1/events?since=150", headers=_auth())
    kinds = [e["kind"] for e in r.json()["events"]]
    assert kinds == ["cron_completed", "agent_message"]


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
    tss = [e["ts"] for e in r.json()["events"]]
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
    tss = [e["ts"] for e in r.json()["events"]]
    assert tss == [300.0]


# --------------------------------------------------------------- limit


def test_events_limit_keeps_most_recent(client: TestClient) -> None:
    """``limit=N`` keeps the most recent N events, matching
    ``EventLog.read``'s behaviour. A falling-behind poller that
    catches up with a bounded request sees the newest slice."""
    _seed(
        client,
        kinds_and_ts=[(f"kind_{i}", float(i)) for i in range(10)],
    )
    r = client.get("/v1/events?limit=3", headers=_auth())
    kinds = [e["kind"] for e in r.json()["events"]]
    assert kinds == ["kind_7", "kind_8", "kind_9"]


def test_events_limit_rejects_zero(client: TestClient) -> None:
    r = client.get("/v1/events?limit=0", headers=_auth())
    assert r.status_code == 400


def test_events_limit_rejects_huge(client: TestClient) -> None:
    r = client.get("/v1/events?limit=10000", headers=_auth())
    assert r.status_code == 400
