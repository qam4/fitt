"""Tests for ``/v1/approvals/pending`` and
``/v1/approvals/{id}/decide``.

Covers:
- List returns only approvals routed to the requester's client
  (via ``?client=`` filter).
- Decide resolves the future; 404 for unknown id; 403 for a token
  whose client tag doesn't match the approval's target client.
- Full lifecycle: request → list → decide → resolved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import AllowedToken, Secrets
from gateway.projects import ProjectRegistry
from gateway.tools import ApprovalBucket, Tool, ToolContext, ToolResult
from tests._fixtures import build_test_config

_IDE_TOKEN = "IDE_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_TELEGRAM_TOKEN = "TELEGRAM_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAA"


async def _noop(args: dict, ctx: ToolContext) -> ToolResult:
    raise AssertionError("stub should never be executed by endpoint tests")


def _mk_tool(name: str, bucket: ApprovalBucket = ApprovalBucket.ASK) -> Tool:
    return Tool(
        name=name,
        description=f"stub {name}",
        schema={"type": "object", "properties": {}},
        callable=_noop,
        default_bucket=bucket,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Gateway with two tagged tokens: one `ide`, one `telegram`.

    No generic `webui` token so cross-client tests have a clear
    separation.
    """
    cfg = build_test_config(tmp_path)
    cfg.secrets = Secrets(
        allowed_tokens=[
            AllowedToken(name="ide-token", token=_IDE_TOKEN, client="ide"),
            AllowedToken(name="telegram-token", token=_TELEGRAM_TOKEN, client="telegram"),
        ],
        openrouter_api_key="sk-or-test-xxxxx",
    )
    app = create_app(cfg)
    # Inject a throwaway tool so approvals can be created.
    app.state.tool_registry.register(_mk_tool("edit_file", ApprovalBucket.ASK))
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- list


def test_list_pending_empty(client: TestClient) -> None:
    r = client.get("/v1/approvals/pending", headers=_auth(_TELEGRAM_TOKEN))
    assert r.status_code == 200
    assert r.json() == {"pending": []}


def test_list_pending_unauthenticated(client: TestClient) -> None:
    r = client.get("/v1/approvals/pending")
    assert r.status_code == 401


def test_list_filters_by_client(client: TestClient, tmp_path: Path) -> None:
    """Telegram poller asking for ?client=telegram only sees
    telegram-routed approvals; ide-routed approvals are invisible.
    """
    app = client.app
    approval = app.state.approval
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")

    async def seed() -> tuple[str, str]:
        tool = app.state.tool_registry.lookup("edit_file")
        tg = await approval.request_approval(
            tool,
            {"path": "a.py"},
            ToolContext(client="telegram", session_key="main", projects=reg),
        )
        ide = await approval.request_approval(
            tool,
            {"path": "b.py"},
            ToolContext(client="ide", session_key="main", projects=reg),
        )
        return tg.approval_id, ide.approval_id

    tg_id, ide_id = asyncio.run(seed())
    try:
        # The telegram token sees both when unfiltered (auth doesn't
        # force the filter — the poller has to supply it).
        r = client.get("/v1/approvals/pending", headers=_auth(_TELEGRAM_TOKEN))
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()["pending"]}
        assert ids == {tg_id, ide_id}

        # With ?client=telegram, only the telegram-bound one.
        r = client.get("/v1/approvals/pending?client=telegram", headers=_auth(_TELEGRAM_TOKEN))
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()["pending"]}
        assert ids == {tg_id}

        # ?client=ide works for any authenticated caller. The
        # 403-for-client-mismatch is on decide, not list.
        r = client.get("/v1/approvals/pending?client=ide", headers=_auth(_TELEGRAM_TOKEN))
        ids = {p["id"] for p in r.json()["pending"]}
        assert ids == {ide_id}
    finally:
        # Resolve the seeded approvals so the test doesn't leak
        # never-awaited futures.
        asyncio.run(_drain(approval, [tg_id, ide_id]))


def test_list_returns_age_and_summary(client: TestClient, tmp_path: Path) -> None:
    app = client.app
    approval = app.state.approval
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")

    async def seed() -> str:
        tool = app.state.tool_registry.lookup("edit_file")
        p = await approval.request_approval(
            tool,
            {"path": "foo.py", "message": "fix"},
            ToolContext(client="telegram", session_key="main", projects=reg),
        )
        return p.approval_id

    ap_id = asyncio.run(seed())
    try:
        r = client.get("/v1/approvals/pending?client=telegram", headers=_auth(_TELEGRAM_TOKEN))
        pending = r.json()["pending"]
        assert len(pending) == 1
        entry = pending[0]
        assert entry["id"] == ap_id
        assert entry["tool"] == "edit_file"
        assert entry["client"] == "telegram"
        assert entry["session"] == "main"
        assert "path='foo.py'" in entry["args_summary"]
        assert entry["age_s"] >= 0.0
    finally:
        asyncio.run(_drain(approval, [ap_id]))


# --------------------------------------------------------------- decide


def test_decide_unknown_id_returns_404(client: TestClient) -> None:
    r = client.post(
        "/v1/approvals/nope-not-a-real-id/decide",
        headers=_auth(_TELEGRAM_TOKEN),
        json={"decision": "approve"},
    )
    assert r.status_code == 404


def test_decide_blocks_cross_client(client: TestClient, tmp_path: Path) -> None:
    """IDE token cannot approve a prompt targeted at Telegram."""
    app = client.app
    approval = app.state.approval
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")

    async def seed() -> str:
        tool = app.state.tool_registry.lookup("edit_file")
        p = await approval.request_approval(
            tool,
            {},
            ToolContext(client="telegram", session_key="main", projects=reg),
        )
        return p.approval_id

    ap_id = asyncio.run(seed())
    try:
        # IDE token tries to decide the telegram-bound approval.
        r = client.post(
            f"/v1/approvals/{ap_id}/decide",
            headers=_auth(_IDE_TOKEN),
            json={"decision": "approve"},
        )
        assert r.status_code == 403
        # The approval is still pending (not resolved).
        pending_after = asyncio.run(approval.list_pending())
        assert any(p.approval_id == ap_id for p in pending_after)
    finally:
        asyncio.run(_drain(approval, [ap_id]))


def test_decide_resolves_future(client: TestClient, tmp_path: Path) -> None:
    app = client.app
    approval = app.state.approval
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")

    async def seed_and_wait() -> tuple[str, asyncio.Future[str]]:
        tool = app.state.tool_registry.lookup("edit_file")
        p = await approval.request_approval(
            tool,
            {},
            ToolContext(client="telegram", session_key="main", projects=reg),
        )
        return p.approval_id, p.future

    ap_id, fut = asyncio.run(seed_and_wait())

    r = client.post(
        f"/v1/approvals/{ap_id}/decide",
        headers=_auth(_TELEGRAM_TOKEN),
        json={"decision": "approve"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "resolved": True}

    # Future is set (future.done() + the string was "approve").
    assert fut.done()
    assert fut.result() == "approve"


def test_decide_bad_body_is_422(client: TestClient) -> None:
    """Pydantic validation rejects an unknown decision string."""
    r = client.post(
        "/v1/approvals/any-id/decide",
        headers=_auth(_TELEGRAM_TOKEN),
        json={"decision": "maybe"},
    )
    # Pydantic Literal validation → 422.
    assert r.status_code == 422


async def _drain(approval: object, ids: list[str]) -> None:
    """Resolve any leftover pending approvals so their futures don't
    raise 'never awaited' at teardown."""
    for ap_id in ids:
        await approval.resolve_approval(ap_id, "reject")  # type: ignore[attr-defined]
