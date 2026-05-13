"""Tests for the request-id middleware.

Pins the cross-log-correlation contract: every chat request
flows through the middleware, every structured log event
written for that request carries the same ``request_id``,
and the same id is echoed back as a response header so the
caller (the Telegram bot, primarily) can join its own logs
to the gateway's by id.

The middleware itself is tiny — read header, validate or
generate, bind contextvar, echo on response — but the
guarantees it gives us are load-bearing for operational
debugging, so we pin them tightly here:

1. **Inbound id passes through.** A well-formed
   ``X-Request-Id`` from the client lands on
   ``request.state.request_id`` AND is echoed in the
   response. The bot relies on the echo to confirm the
   id it sent is the one the gateway actually used.
2. **Missing id gets generated.** No header → fresh
   UUID, still echoed in the response, still bound to
   contextvars.
3. **Malformed id gets replaced.** A header value that
   fails validation (too short, control characters,
   absurdly long) is silently substituted with a fresh
   UUID — we don't want a malicious or buggy client
   poisoning the structured-log key with megabytes of
   garbage or with a value that looks like a header
   injection attempt.
4. **Contextvar reaches the structured log.** A chat
   completion log event written during the request
   carries ``request_id`` as a structured field via
   ``structlog.contextvars.merge_contextvars``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.request_id import HEADER_NAME, _is_valid, _new_request_id

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def captured_log_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Spy on ``gateway.chat.log_request`` so we can assert
    the contextvar reached the chat-completion log payload
    without configuring the global logging chain. Mirrors the
    pattern in ``test_chat_error_logging.py``."""
    events: list[dict[str, Any]] = []

    def fake_log_request(_logger: Any, **fields: Any) -> None:
        # Pull contextvars in via structlog's binding so the
        # spy sees the same ``request_id`` field a real log
        # emission would carry.
        import structlog

        merged = dict(structlog.contextvars.get_contextvars())
        merged.update(fields)
        events.append(merged)

    monkeypatch.setattr("gateway.chat.log_request", fake_log_request)
    yield events


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# ----------------------------------------------------- helpers


def test_is_valid_accepts_uuid_hex() -> None:
    """uuid4().hex is the canonical id; must always validate."""
    rid = _new_request_id()
    assert _is_valid(rid)


def test_is_valid_accepts_dashed_uuid() -> None:
    """Many tracing systems format UUIDs with dashes
    (envoy default for example). Validate them too —
    dashes are explicitly allowed by the regex."""
    assert _is_valid("01234567-89ab-cdef-0123-456789abcdef")


def test_is_valid_rejects_too_short() -> None:
    """Anything below 8 chars is suspicious enough that we'd
    rather generate a fresh id than trust it."""
    assert not _is_valid("abc123")


def test_is_valid_rejects_too_long() -> None:
    """A megabyte-long header is never a real request id."""
    assert not _is_valid("a" * 200)


def test_is_valid_rejects_control_chars() -> None:
    """Control chars / newlines could break log parsers
    downstream — reject so log files stay newline-delimited
    JSON."""
    assert not _is_valid("abc\n123def")
    assert not _is_valid("abc\x00123def")


def test_is_valid_rejects_special_chars() -> None:
    """Slashes, spaces, semicolons and the like aren't part
    of real request ids and could confuse structured-log
    consumers."""
    assert not _is_valid("abc/123/def")
    assert not _is_valid("abc 123 def")


# ----------------------------------------------------- middleware behaviour


def test_health_endpoint_echoes_request_id_when_provided(client: TestClient) -> None:
    """Even auth-exempt endpoints get a request_id and echo
    it. Health checks aren't typically a hot debugging path
    but consistency is cheap."""
    sent = "test-id-12345678"
    r = client.get("/health", headers={HEADER_NAME: sent})
    assert r.status_code == 200
    assert r.headers[HEADER_NAME] == sent


def test_health_endpoint_generates_request_id_when_absent(client: TestClient) -> None:
    """No header in → fresh id out. The response header is
    always set so the caller can scrape it from anywhere
    they need to."""
    r = client.get("/health")
    assert r.status_code == 200
    rid = r.headers.get(HEADER_NAME)
    assert rid is not None
    assert _is_valid(rid)


def test_health_endpoint_replaces_malformed_request_id(client: TestClient) -> None:
    """Malformed header value → silently substituted with a
    valid generated id. Important for log integrity: a
    bogus header value must never end up as the
    ``request_id`` field in a JSON log line."""
    r = client.get("/health", headers={HEADER_NAME: "x"})  # too short
    assert r.status_code == 200
    rid = r.headers[HEADER_NAME]
    assert rid != "x"
    assert _is_valid(rid)


def test_chat_completion_log_carries_inbound_request_id(
    client: TestClient,
    captured_log_events: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the middleware: a chat-completion
    log event written during the request carries the
    inbound ``X-Request-Id`` value. Pin via the spy so we
    don't have to reach into the global logging chain."""
    from ._llm_stubs import stub_reply

    async def fake(**_: Any) -> Any:
        return stub_reply("hi back")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    sent = "request-id-from-bot-aaaaaaaaaaaa"
    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-default", "messages": [{"role": "user", "content": "hi"}]},
        headers={**_auth(), HEADER_NAME: sent},
    )
    assert r.status_code == 200
    # Same id echoed back — the bot's contract.
    assert r.headers[HEADER_NAME] == sent

    # And the log row — the gateway operator's contract.
    assert len(captured_log_events) == 1
    assert captured_log_events[0]["request_id"] == sent


def test_chat_completion_log_carries_generated_request_id(
    client: TestClient,
    captured_log_events: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the bot doesn't send a header (older bot, manual
    curl, broken middleware in front of us), the gateway
    generates an id and uses it everywhere — log AND
    response header. The bot can still grep using the
    response value even if it didn't send one in."""
    from ._llm_stubs import stub_reply

    async def fake(**_: Any) -> Any:
        return stub_reply("hi back")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-default", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 200
    rid = r.headers[HEADER_NAME]
    assert _is_valid(rid)

    assert len(captured_log_events) == 1
    assert captured_log_events[0]["request_id"] == rid
