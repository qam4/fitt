"""Tests for ``POST /v1/eval/<alias>`` — Phase 7 Slice 7.3.

Three concerns:

* Shape: the endpoint returns the documented summary fields
  (alias, model_id, started/finished, pass/fail counts,
  pass_rate, per-case detail, rendered markdown).
* Aggregation: when the harness runs, the response reflects
  what cases ran and their outcomes. Tests don't exercise the
  full default suite (5 real network calls per test) — they
  patch ``run_eval_suite`` to return a synthetic report.
* Auth + 404: bearer-gated; unknown alias returns 404 with a
  clean error envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gateway.alias_eval import CaseResult, EvalReport
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


def _make_report(alias: str = "fitt-default") -> EvalReport:
    """Synthetic report with two cases (one pass, one
    narrated) so tests can assert on aggregation without
    running real LLM calls."""
    started = datetime(2026, 5, 22, 19, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 5, 22, 19, 0, 30, tzinfo=UTC)
    return EvalReport(
        alias=alias,
        model_id="qwen2.5-coder:14b",
        started_at=started,
        finished_at=finished,
        cases=[
            CaseResult(
                case_name="read_file_basic",
                status="pass",
                detail="called 'read_file' as expected",
                latency_ms=120,
                tool_called="read_file",
                finish_reason="tool_calls",
            ),
            CaseResult(
                case_name="grep_repo_basic",
                status="narrated",
                detail="model replied with 247 chars instead of emitting tool_calls",
                latency_ms=1500,
                finish_reason="stop",
                reply_preview="Sure, I'll grep for...",
            ),
        ],
    )


# --------------------------------------------------------------- auth


def test_eval_requires_bearer(client: TestClient) -> None:
    r = client.post("/v1/eval/fitt-default")
    assert r.status_code == 401


# --------------------------------------------------------------- 404


def test_eval_unknown_alias_returns_404(client: TestClient) -> None:
    r = client.post("/v1/eval/nonexistent", headers=_auth())
    assert r.status_code == 404
    body = r.json()
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert detail["error"]["type"] == "unknown_alias"
    # Available list is included so the operator sees what
    # they could have meant.
    assert "available" in detail["error"]


# --------------------------------------------------------------- success


def test_eval_returns_summary_shape(client: TestClient) -> None:
    """Patch the suite to return a synthetic report so the
    test runs in milliseconds. The endpoint's responsibility
    is wrapping the runner; the runner's tests live in
    test_alias_eval.py."""
    report = _make_report("fitt-default")

    async def _stub(*_args: Any, **_kwargs: Any) -> EvalReport:
        return report

    with patch("gateway.eval_endpoint.run_eval_suite", side_effect=_stub):
        r = client.post("/v1/eval/fitt-default", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    # Documented top-level fields.
    assert body["alias"] == "fitt-default"
    assert body["model_id"] == "qwen2.5-coder:14b"
    assert body["passed"] == 1
    assert body["failed"] == 1
    assert body["total"] == 2
    assert body["pass_rate"] == pytest.approx(0.5)
    assert body["duration_ms"] == 30_000
    assert "started_at" in body
    assert "finished_at" in body
    # Per-case detail.
    assert len(body["cases"]) == 2
    names = [c["name"] for c in body["cases"]]
    assert names == ["read_file_basic", "grep_repo_basic"]
    statuses = [c["status"] for c in body["cases"]]
    assert statuses == ["pass", "narrated"]
    # Markdown rendering included for clients that prefer it.
    assert "markdown" in body
    assert "Eval report" in body["markdown"]
    assert "fitt-default" in body["markdown"]


def test_eval_persists_rolling_report(tmp_path: Path) -> None:
    """The endpoint persists the rolling per-alias report so
    /v1/aliases's last_eval lookup picks it up. This pins the
    contract Slice 7.1 depends on."""
    from gateway.alias_eval import default_eval_dir
    from gateway.config import fitt_home

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    report = _make_report("fitt-default")

    async def _stub(*_args: Any, **_kwargs: Any) -> EvalReport:
        return report

    with patch("gateway.eval_endpoint.run_eval_suite", side_effect=_stub):
        r = client.post("/v1/eval/fitt-default", headers=_auth())
    assert r.status_code == 200, r.text

    # Rolling report persisted at <fitt_home>/eval/<alias>-latest.md.
    eval_dir = default_eval_dir(fitt_home())
    rolling = eval_dir / "fitt-default-latest.md"
    assert rolling.exists()
    assert "fitt-default" in rolling.read_text(encoding="utf-8")


def test_eval_handles_runner_exception_returns_500(client: TestClient) -> None:
    """If the runner itself raises (infrastructure failure,
    not per-case dispatch failure), the endpoint surfaces a
    500 with a typed error envelope rather than a stack
    trace."""
    with patch(
        "gateway.eval_endpoint.run_eval_suite",
        side_effect=RuntimeError("harness broke"),
    ):
        r = client.post("/v1/eval/fitt-default", headers=_auth())
    assert r.status_code == 500
    body = r.json()
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert detail["error"]["type"] == "eval_infrastructure_failure"


# --------------------------------------------------------------- helpers
# (None needed; tests use inline async stubs as side_effect.)
