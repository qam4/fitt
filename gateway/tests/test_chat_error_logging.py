"""Pin that chat-completion failure paths emit structured logs.

Pre-2026-05-13, an upstream rate-limit / queue / connection-reset
during a chat dispatch returned a translated 503/502 to the bot
without writing anything to ``gateway.log``. The Telegram user
saw "gateway unreachable" and the operator had no log to grep —
the request had simply vanished from the structured stream.

These tests pin the post-fix contract: every chat request emits
exactly one ``chat.completion`` event regardless of outcome,
with classification fields (``error_type``, ``error_class``,
``error_detail``, ``upstream_status``) populated for failure
paths so an operator can ``jq 'select(.event=="chat.completion"
and .error_type=="upstream_rate_limited")'`` to find them.

We exercise four kinds of failure here:

1. **Translated 429 from upstream** — a status-bearing exception
   with ``status_code=429`` raised mid-dispatch. Maps to
   ``status=upstream_rate_limited``.
2. **Translated 529 (overload)** — same bucket as 429.
3. **Connection failure** — an ``httpx.ConnectError`` raised
   mid-dispatch; the router exhausts all candidates and raises
   ``NoBackendAvailable``. Maps to
   ``status=no_backend_available`` with the underlying
   transport exception's class (``ConnectError``,
   ``ReadTimeout``, ...) in ``error_class``.
4. **4xx other** — a 401 from a misconfigured upstream API
   key. Maps to ``status=upstream_client_error`` with the
   HTTP status preserved.

The tests spy on ``gateway.chat.log_request`` rather than
configuring the global logging chain, so they don't disturb
later tests in the suite that rely on pytest's default
stdout/stderr capture (notably ``test_log_bodies``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def captured_log_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Spy on ``gateway.chat.log_request`` so tests can assert
    what fields the chat handler tried to log without configuring
    the real (idempotent, global, stateful) logging chain.

    Mirrors the actual ``log_request`` signature: the first
    positional arg is the logger; remaining kwargs are the
    structured event payload. We record only the kwargs since
    that's the schema operators search by.
    """
    events: list[dict[str, Any]] = []

    def fake_log_request(_logger: Any, **fields: Any) -> None:
        events.append(dict(fields))

    monkeypatch.setattr("gateway.chat.log_request", fake_log_request)
    yield events


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _completion_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return events that look like a chat-completion log row.

    Every ``log_request`` call emits a ``chat.completion`` event,
    so all spied calls are in scope. Returning the full list lets
    the assertions count them too — pinning the
    "exactly-one-per-request" invariant.
    """
    return events


# ----------------------------------------------------- 429 / rate limit


class _StatusError(Exception):
    """Mimic the shape LiteLLM raises on upstream HTTP errors:
    a ``status_code`` attribute and a ``message``. The
    classification code in chat.py reads exactly these fields,
    plus optional ``response.headers['retry-after']``."""

    def __init__(
        self,
        status_code: int,
        message: str,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message

        class _Resp:
            def __init__(self, ra: str | None) -> None:
                self.headers = {"retry-after": ra} if ra is not None else {}

        self.response = _Resp(retry_after)


def test_429_emits_chat_completion_with_rate_limited_status(
    client: TestClient,
    captured_log_events: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_: Any) -> Any:
        raise _StatusError(status_code=429, message="rate limit", retry_after="30")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    # Gateway translates 429 → 503 + Retry-After.
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "upstream_rate_limited"

    events = _completion_events(captured_log_events)
    assert len(events) == 1, events
    e = events[0]
    assert e["status"] == "upstream_rate_limited"
    assert e["upstream_status"] == 429
    assert e["error_class"] == "_StatusError"
    assert "rate limit" in e["error_detail"]
    assert e["alias"] == "fitt-smart"
    assert e["retry_after"] == "30"


def test_503_overload_emits_chat_completion_with_rate_limited_status(
    client: TestClient,
    captured_log_events: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """529 (Anthropic-style overload) maps to the same
    ``upstream_rate_limited`` classification as 429 — operators
    typically want to grep them together."""

    async def fake(**_: Any) -> Any:
        raise _StatusError(status_code=529, message="overloaded")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 503

    events = _completion_events(captured_log_events)
    assert len(events) == 1
    assert events[0]["status"] == "upstream_rate_limited"
    assert events[0]["upstream_status"] == 529


# ----------------------------------------------------- connection failure


def test_connect_error_emits_chat_completion_with_no_backend_status(
    client: TestClient,
    captured_log_events: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-level failure (DNS, connect, read timeout) is
    caught by the router, which exhausts the candidate chain
    and raises ``NoBackendAvailable`` — translated to a 503
    ``no_backend_available`` for the client by the app-level
    exception handler.

    The chat handler still has to emit a ``chat.completion``
    event for the failed turn so an operator can see what
    actually happened — pre-fix, this path silently raised and
    the request vanished from ``gateway.log``.

    The event carries ``status=no_backend_available`` plus the
    underlying transport exception's class and detail so DNS
    failures, connect-refused, and read-timeouts can be told
    apart."""
    import httpx

    async def fake(**_: Any) -> Any:
        raise httpx.ConnectError("Name or service not known")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_backend_available"

    events = _completion_events(captured_log_events)
    assert len(events) == 1
    e = events[0]
    assert e["status"] == "no_backend_available"
    assert e["error_class"] == "ConnectError"
    assert "Name or service not known" in e["error_detail"]
    assert e["alias"] == "fitt-smart"


# ----------------------------------------------------- 4xx other


def test_401_emits_chat_completion_with_client_error_status(
    client: TestClient,
    captured_log_events: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx that isn't rate-limit (e.g. 401 from misconfigured
    upstream API key) maps to ``upstream_client_error``. The
    HTTP status passes through verbatim so the bot can
    distinguish 'auth' from 'rate limit' from 'gateway down'."""

    async def fake(**_: Any) -> Any:
        raise _StatusError(status_code=401, message="bad api key")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 401

    events = _completion_events(captured_log_events)
    assert len(events) == 1
    assert events[0]["status"] == "upstream_client_error"
    assert events[0]["upstream_status"] == 401
