"""Tests for ``GET /v1/status`` — Phase 7 Slice 7.3.

Three concerns:

* Shape: every documented field is present, even when the
  underlying subsystem is empty (no MCP servers, no cron jobs,
  no gaps).
* Aggregation: counts come from the right sources (cron from
  CronService, gaps from CapabilityGapLog, MCP from manager).
* Auth: bearer-gated — same posture as ``/v1/aliases``.

The endpoint must never raise on a missing or partially-wired
subsystem; degraded values (zero counts, ``None`` timestamps)
are the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import fitt_home

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


def test_status_requires_bearer(client: TestClient) -> None:
    r = client.get("/v1/status")
    assert r.status_code == 401


def test_status_returns_200_with_valid_bearer(client: TestClient) -> None:
    r = client.get("/v1/status", headers=_auth())
    assert r.status_code == 200


# --------------------------------------------------------------- shape


def test_status_includes_all_top_level_fields(client: TestClient) -> None:
    """Every documented field is present — the dashboard /
    Telegram command can render reliably."""
    r = client.get("/v1/status", headers=_auth())
    body = r.json()

    assert "generated_at" in body
    assert isinstance(body["generated_at"], (int, float))

    assert "gateway" in body
    assert "uptime_s" in body["gateway"]
    assert "started_at" in body["gateway"]

    assert "mcp" in body
    assert "servers_total" in body["mcp"]
    assert "servers_running" in body["mcp"]

    assert "cron" in body
    assert "total" in body["cron"]
    assert "enabled" in body["cron"]
    assert "next_firing" in body["cron"]

    assert "capability_gaps" in body
    assert "total" in body["capability_gaps"]

    assert "pruners" in body
    assert "history_last_sweep" in body["pruners"]
    assert "events_last_sweep" in body["pruners"]

    assert "telegram" in body
    assert "configured" in body["telegram"]


def test_status_uptime_is_positive(client: TestClient) -> None:
    """Uptime is a positive float since the gateway started
    before the test request reached it."""
    r = client.get("/v1/status", headers=_auth())
    body = r.json()
    assert body["gateway"]["uptime_s"] >= 0


def test_status_empty_session_zero_counts(client: TestClient) -> None:
    """Fresh app with no cron jobs, no gaps, no MCP servers
    returns zeros rather than nulls or missing keys."""
    r = client.get("/v1/status", headers=_auth())
    body = r.json()
    assert body["cron"]["total"] == 0
    assert body["cron"]["enabled"] == 0
    assert body["cron"]["next_firing"] is None
    assert body["capability_gaps"]["total"] == 0
    assert body["mcp"]["servers_total"] == 0
    assert body["mcp"]["servers_running"] == 0
    assert body["pruners"]["history_last_sweep"] is None
    assert body["pruners"]["events_last_sweep"] is None


def test_status_telegram_unconfigured_in_test_fixture(client: TestClient) -> None:
    """build_test_config doesn't wire telegram secrets so the
    flag is False — the Telegram bot doesn't have to be
    running for /v1/status to make sense."""
    r = client.get("/v1/status", headers=_auth())
    body = r.json()
    assert body["telegram"]["configured"] is False


# --------------------------------------------------------------- aggregation


def test_status_picks_up_capability_gaps(tmp_path: Path) -> None:
    """The gap count surfaces from the running CapabilityGapLog."""
    from gateway.capabilities import GapReport

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    # Append a couple of gap reports through the live store so
    # the test exercises the same read path as the endpoint.
    app.state.capability_gaps.append(
        GapReport(ts=1779479823.0, session_key="main", action="X", suggestion="add Y")
    )
    app.state.capability_gaps.append(
        GapReport(ts=1779479824.0, session_key="main", action="X", suggestion="add Y")
    )

    r = client.get("/v1/status", headers=_auth())
    body = r.json()
    assert body["capability_gaps"]["total"] == 2


def test_status_reads_pruner_anchor_files(tmp_path: Path) -> None:
    """When the history pruner has run, its anchor file holds
    the last-sweep timestamp; /v1/status surfaces it."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    home = fitt_home()
    history_anchor = home / "history.pruner.anchor"
    history_anchor.parent.mkdir(parents=True, exist_ok=True)
    history_anchor.write_text("1779479823.42", encoding="utf-8")

    r = client.get("/v1/status", headers=_auth())
    body = r.json()
    assert body["pruners"]["history_last_sweep"] == 1779479823.42


def test_status_handles_corrupt_anchor_file(tmp_path: Path) -> None:
    """A non-numeric anchor file degrades to None rather than
    crashing the endpoint."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    home = fitt_home()
    bad_anchor = home / "history.pruner.anchor"
    bad_anchor.parent.mkdir(parents=True, exist_ok=True)
    bad_anchor.write_text("not a timestamp", encoding="utf-8")

    r = client.get("/v1/status", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["pruners"]["history_last_sweep"] is None


def test_status_picks_up_cron_jobs(tmp_path: Path) -> None:
    """When CronService has jobs, the endpoint surfaces totals
    and the next firing timestamp."""
    from gateway.cron import CronJob, CronSchedule

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    enabled_schedule = CronSchedule(kind="cron", cron_expr="*/5 * * * *", timezone="UTC")
    disabled_schedule = CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="UTC")

    app.state.cron.add(
        CronJob(
            id="cron-1",
            name="probe",
            message="hi",
            schedule=enabled_schedule,
            enabled=True,
            created_ts=1779479823.0,
        )
    )
    app.state.cron.add(
        CronJob(
            id="cron-2",
            name="paused",
            message="bye",
            schedule=disabled_schedule,
            enabled=False,
            created_ts=1779479824.0,
        )
    )

    r = client.get("/v1/status", headers=_auth())
    body = r.json()
    assert body["cron"]["total"] == 2
    assert body["cron"]["enabled"] == 1
    # next_firing should be a float since we have an enabled
    # cron expression that fires every 5 minutes.
    assert isinstance(body["cron"]["next_firing"], (int, float))
