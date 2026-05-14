"""Tests for the gateway HTTP client."""

from __future__ import annotations

import httpx
import respx

from fitt_telegram_bot.gateway_client import GatewayClient


def _client() -> GatewayClient:
    return GatewayClient("http://127.0.0.1:8080", "TEST_TOKEN")


async def _collect(aiter) -> list[str]:
    return [x async for x in aiter]


async def test_list_aliases_parses_response() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8080/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "fitt-default", "object": "model"},
                        {"id": "fitt-smart", "object": "model"},
                    ],
                },
            )
        )
        aliases = await _client().list_aliases()
    assert aliases == ["fitt-default", "fitt-smart"]


async def test_list_aliases_tolerates_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8080/v1/models").mock(side_effect=httpx.ConnectError("boom"))
        aliases = await _client().list_aliases()
    assert aliases == []


async def test_chat_streams_deltas() -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse, headers={"Content-Type": "text/event-stream"}
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert deltas == ["Hel", "lo"]


async def test_chat_rate_limited_surface() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                503,
                json={"error": {"message": "too busy"}},
                headers={"retry-after": "7"},
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "Rate limited" in deltas[0]
    assert "7" in deltas[0]


async def test_chat_unauthorised_surface() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad token"}})
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "401" in deltas[0]


async def test_chat_unreachable_yields_error_string() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("nope")
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "unreachable" in deltas[0]


async def test_chat_upstream_stream_abort() -> None:
    sse = b"data: [ERROR]\n\n"
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse, headers={"Content-Type": "text/event-stream"}
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert "Upstream stopped responding mid-reply" in deltas[0]


async def test_chat_request_includes_tool_choice_auto() -> None:
    """Bot opts every chat request into the gateway's tool loop by
    sending `tool_choice: "auto"`. This is the signal the gateway
    uses to decide whether to inject its registered tools and run
    the tool-execution loop."""
    import json as _json

    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(side_effect=_record)
        # Drain the iterator so the request actually fires.
        async for _ in _client().chat(
            messages=[{"role": "user", "content": "hi"}],
            alias="fitt-smart",
            session_id="main",
        ):
            pass

    body = captured["body"]
    assert isinstance(body, dict)
    assert body.get("tool_choice") == "auto"
    # Backwards-compat: other keys still present.
    assert body.get("model") == "fitt-smart"
    assert body.get("stream") is True


async def test_chat_request_skips_tool_choice_when_disabled() -> None:
    """Tests / debug mode can pass `enable_tools=False` to bypass
    the tool loop entirely. Used for isolating chat behaviour from
    tool-call plumbing when debugging."""
    import json as _json

    from fitt_telegram_bot.gateway_client import GatewayClient

    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    client = GatewayClient("http://127.0.0.1:8080", "TEST_TOKEN", enable_tools=False)
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(side_effect=_record)
        async for _ in client.chat(
            messages=[{"role": "user", "content": "hi"}],
            alias="fitt-smart",
            session_id="main",
        ):
            pass

    body = captured["body"]
    assert isinstance(body, dict)
    assert "tool_choice" not in body


# ----------------------------------------------------- structured failure logs


async def test_chat_unreachable_logs_structured_failure() -> None:
    """Pre-2026-05-13 the bot swallowed transport errors silently —
    the user saw '⚠️ gateway unreachable' in Telegram with no
    matching row in ``telegram-bot.log``. After the fix, every
    yielded ⚠️ has a corresponding ``gateway.chat.failed`` event
    so an operator can grep the log to confirm what actually
    happened. This test pins that contract for transport
    failures (DNS / connect refused / read timeout)."""
    import structlog

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("nope")
        )
        with structlog.testing.capture_logs() as captured:
            deltas = await _collect(
                _client().chat(
                    messages=[{"role": "user", "content": "hi"}],
                    alias="fitt-smart",
                    session_id="main",
                )
            )

    # User-visible warning still surfaces.
    assert deltas == [d for d in deltas if d.startswith("⚠️")]
    # And the structured event landed.
    failed = [e for e in captured if e.get("event") == "gateway.chat.failed"]
    assert len(failed) == 1, captured
    e = failed[0]
    assert e["alias"] == "fitt-smart"
    assert e["session_id"] == "main"
    assert e["failure_kind"] == "connect_failure"
    assert e["error_class"] == "ConnectError"
    assert "nope" in e["error"]


async def test_chat_rate_limited_logs_structured_failure() -> None:
    """A 503 with a Retry-After is the most common upstream
    rate-limit / queue-overflow shape. The structured log row
    carries the upstream status, the parsed ``error.type`` from
    the gateway body, and the Retry-After so an operator can
    correlate the bot-side row with the gateway-side
    ``chat.completion`` event by matching on those fields."""
    import structlog

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                503,
                json={
                    "error": {
                        "message": "too busy",
                        "type": "upstream_rate_limited",
                    }
                },
                headers={"retry-after": "7"},
            )
        )
        with structlog.testing.capture_logs() as captured:
            await _collect(
                _client().chat(
                    messages=[{"role": "user", "content": "hi"}],
                    alias="fitt-smart",
                    session_id="main",
                )
            )

    failed = [e for e in captured if e.get("event") == "gateway.chat.failed"]
    assert len(failed) == 1
    e = failed[0]
    assert e["failure_kind"] == "http_error"
    assert e["upstream_status"] == 503
    assert e["error_type"] == "upstream_rate_limited"
    assert "too busy" in e["error_detail"]
    assert e["retry_after"] == "7"


async def test_chat_unauthorised_logs_structured_failure() -> None:
    """401 from a misconfigured Bearer token surfaces with the
    parsed gateway error type so the bot's failure stream and
    the gateway's ``chat.completion`` event can be joined on
    ``upstream_status=401`` without timestamp guesswork."""
    import structlog

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"message": "bad token", "type": "invalid_auth"}},
            )
        )
        with structlog.testing.capture_logs() as captured:
            await _collect(
                _client().chat(
                    messages=[{"role": "user", "content": "hi"}],
                    alias="fitt-smart",
                    session_id="main",
                )
            )

    failed = [e for e in captured if e.get("event") == "gateway.chat.failed"]
    assert len(failed) == 1
    e = failed[0]
    assert e["upstream_status"] == 401
    assert e["error_type"] == "invalid_auth"
    assert "bad token" in e["error_detail"]


async def test_chat_stream_abort_logs_structured_failure() -> None:
    """The mid-stream ``[ERROR]`` marker is the gateway telling
    the bot the upstream died after streaming started. The bot
    logs ``failure_kind=stream_aborted`` to mirror the gateway's
    own ``status=stream_failure`` ``chat.completion`` event;
    same incident, two log files, joinable by alias +
    session_id + timestamp."""
    import structlog

    sse = b"data: [ERROR]\n\n"
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse, headers={"Content-Type": "text/event-stream"}
            )
        )
        with structlog.testing.capture_logs() as captured:
            await _collect(
                _client().chat(
                    messages=[{"role": "user", "content": "hi"}],
                    alias="fitt-smart",
                    session_id="main",
                )
            )

    failed = [e for e in captured if e.get("event") == "gateway.chat.failed"]
    assert len(failed) == 1
    assert failed[0]["failure_kind"] == "stream_aborted"
    assert failed[0]["alias"] == "fitt-smart"
    assert failed[0]["session_id"] == "main"


# ----------------------------------------------------- request_id propagation


async def test_chat_sends_x_request_id_header() -> None:
    """Every chat call generates a UUID4 ``X-Request-Id`` and
    sends it on the wire. The gateway side mirrors it back as
    a response header and uses it as the ``request_id`` field
    in every ``chat.completion`` log event written while the
    request runs, so an operator grepping ``telegram-bot.log``
    and ``gateway.log`` for the same id sees the entire turn
    end-to-end."""
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["x-request-id"] = request.headers.get("x-request-id")
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(side_effect=_record)
        async for _ in _client().chat(
            messages=[{"role": "user", "content": "hi"}],
            alias="fitt-smart",
            session_id="main",
        ):
            pass

    rid = captured["x-request-id"]
    # 32 hex chars (uuid4().hex) — never None, never empty.
    assert isinstance(rid, str)
    assert len(rid) == 32
    assert all(c in "0123456789abcdef" for c in rid)


async def test_chat_failure_log_carries_request_id() -> None:
    """The shared ``request_id`` is the join key that lets an
    operator pull a single user-visible warning out of
    ``telegram-bot.log`` and find every gateway-side log row
    for the same turn. This test pins that the structured
    failure event carries the same id sent on the wire."""
    import structlog

    captured_request: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured_request["x-request-id"] = request.headers.get("x-request-id")
        return httpx.Response(
            503,
            json={"error": {"message": "queue full", "type": "upstream_rate_limited"}},
            headers={"retry-after": "5"},
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(side_effect=_record)
        with structlog.testing.capture_logs() as captured_logs:
            await _collect(
                _client().chat(
                    messages=[{"role": "user", "content": "hi"}],
                    alias="fitt-smart",
                    session_id="main",
                )
            )

    failed = [e for e in captured_logs if e.get("event") == "gateway.chat.failed"]
    assert len(failed) == 1
    # Same id on the wire and in the log row.
    assert failed[0]["request_id"] == captured_request["x-request-id"]


# ----------------------------------------------------- upstream_silent (Phase 4.9)


async def test_chat_upstream_silent_translates_to_actionable_message() -> None:
    """The headline Phase 4.9 case. Gateway returns 503 with
    body
    ``{"error": {"type": "upstream_silent", "timeout_secs": 300,
    "alias": "fitt-smart", "request_id": "..."}}``. The bot's
    user-visible message tells the user *what* happened
    (upstream queued, not gateway down), *which* alias, *how
    long* the gateway waited, and includes the short
    request_id tag for cross-log correlation."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                503,
                json={
                    "error": {
                        "type": "upstream_silent",
                        "timeout_secs": 300,
                        "alias": "fitt-smart",
                        "request_id": "abc12345deadbeef",
                        "message": "Upstream went silent after 300s",
                    }
                },
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    msg = deltas[0]
    # Specific shape pinned: tells the user the cause and the
    # remedy, mentions the alias by name, includes the timeout
    # number, and ends with the request_id short tag.
    assert "went silent" in msg
    assert "300s" in msg
    assert "fitt-smart" in msg
    assert "queued" in msg.lower() or "different alias" in msg
    # Short request_id tag at the end. The id printed is the
    # bot's own uuid4().hex (the one sent on the wire as
    # ``X-Request-Id``), not the one echoed in the gateway's
    # error body — the bot trusts what it generated.
    import re

    m = re.search(r"\(req: ([a-f0-9]{8})\)", msg)
    assert m is not None, msg


async def test_chat_no_backend_translates_clearly() -> None:
    """When the gateway returns no_backend_available, the bot
    must surface that distinctly from upstream_silent — a
    gateway that exhausted its candidate chain on
    transport failures is genuinely 'I tried every option'."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                503,
                json={
                    "error": {
                        "type": "no_backend_available",
                        "message": "all candidates failed",
                    }
                },
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "couldn't reach any backend" in deltas[0]
    assert "fitt-smart" in deltas[0]


async def test_chat_user_messages_include_short_request_id() -> None:
    """Every user-facing ⚠️ message ends with a ``(req:
    <8chars>)`` tag so the user can paste it in a bug report
    and the operator can grep both log files. Pinned via the
    upstream_silent path; same code path for every error
    type so this also covers no_backend, rate_limited, etc.

    The bot generates a fresh UUID4 hex per chat call in
    `chat()`; the gateway echoes it as
    `X-Request-Id`. Even when the gateway's response body
    repeats it via `error.request_id`, the bot uses the
    one from the wire (its own generation) so the tag is
    consistent regardless of body shape."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                503,
                json={"error": {"type": "upstream_silent", "timeout_secs": 5}},
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    msg = deltas[0]
    # Find the (req: xxxxxxxx) suffix and verify its 8-char id is
    # 8 hex chars (first 8 of the bot's uuid4().hex).
    import re

    m = re.search(r"\(req: ([a-f0-9]{8})\)", msg)
    assert m is not None, msg
    rid = m.group(1)
    assert len(rid) == 8


async def test_chat_bot_read_timeout_distinct_from_connect_failure() -> None:
    """ReadTimeout means the bot bailed before the gateway
    answered — with the Phase 4.9 invariant in place
    (bot read-timeout > gateway upstream_timeout), this is
    a misconfiguration symptom, not a 'gateway unreachable'
    symptom. The bot's message must call that out so the
    operator goes looking at the timeouts, not at network."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            side_effect=httpx.ReadTimeout("blocked too long")
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    msg = deltas[0]
    assert "didn't respond in time on the bot side" in msg
    assert "Configuration drift" in msg or "read-timeout" in msg
