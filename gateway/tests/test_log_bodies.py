"""Tests for ``server.log_bodies`` debug logging.

Phase 1 specced this flag (requirement 5.3) but it was never
wired — caught during the 2026-05-08 Continue-vs-Telegram
tool-call divergence investigation, where we needed to compare
what each client was actually sending. The flag now lives in
``chat._parse_request``: when set, every incoming chat body is
logged under ``chat.request_body`` with the resolved client
tag.

Invariants pinned here:

* **Off by default.** Nothing leaks without an explicit
  config flip. Honours the "bodies contain user prompts"
  posture from the Phase 1 security notes.
* **On by flag.** Flipping the flag makes the body show up
  in the log with the client tag, making it greppable when
  diagnosing "why does client X behave differently from
  client Y?".
* **Log structure.** The log entry carries ``client`` and
  ``body`` fields so operators can filter by interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.app import create_app

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import stub_reply


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


async def _post_chat(app: Any) -> None:
    """Drive one chat request through the app so _parse_request
    fires."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/v1/chat/completions",
            headers=_auth(),
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            },
        )
        assert r.status_code == 200, r.text


async def test_log_bodies_off_by_default_nothing_logged(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default configuration must NOT log request bodies —
    they contain user prompts."""
    cfg = build_test_config(tmp_path)
    assert cfg.server.log_bodies is False
    app = create_app(cfg)

    async def fake(**_: Any) -> Any:
        return stub_reply("hi there")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await _post_chat(app)

    out, err = capfd.readouterr()
    combined = out + err
    assert "chat.request_body" not in combined, (
        "chat.request_body was logged despite log_bodies=false — "
        "this would leak user prompts to the gateway log. The "
        "Phase 1 spec's 5.3 invariant was 'bodies not logged "
        "unless log_bodies: true.'"
    )


async def test_log_bodies_on_logs_body_with_client_tag(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag enabled, each chat body is logged with the
    resolved client tag so ``grep 'client.*telegram'`` can
    isolate one interface's requests.

    Uses ``capfd`` rather than ``caplog`` because structlog in
    this project writes to a stdout handler that bypasses the
    stdlib ``caplog`` capture. Grabbing the raw stdout shows
    the structured log lines as operators would see them."""
    cfg = build_test_config(tmp_path)
    cfg.server.log_bodies = True
    app = create_app(cfg)

    async def fake(**_: Any) -> Any:
        return stub_reply("hi there")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await _post_chat(app)

    out, err = capfd.readouterr()
    combined = out + err
    assert "chat.request_body" in combined, (
        "chat.request_body was NOT logged despite log_bodies=true; the debug-logging path is broken"
    )
    # The log entry should carry the client tag (default-untagged
    # test token resolves to 'webui').
    assert "client=webui" in combined, (
        "chat.request_body log entry missing client tag; "
        "operators need it to diff one interface against another"
    )
    # Body content should be visible — this is the whole point
    # of the flag.
    assert "fitt-default" in combined, (
        "chat.request_body log entry didn't include the model "
        "field from the body; debug logging isn't showing what "
        "the client sent"
    )
