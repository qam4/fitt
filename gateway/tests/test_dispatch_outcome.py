"""Tests for the shared dispatch-outcome taxonomy (Phase 7.6).

Two concerns:

* Classification correctness: each exception shape maps to the
  right :class:`DispatchStatus`, with error_class / error_detail
  / upstream_status / retry_after populated as documented.
* Totality (property-based): any exception classifies to exactly
  one status; none escape unclassified.

The chat path's equivalence (that its log fields are unchanged
after the extraction) is pinned by the pre-existing
``test_chat_error_logging.py`` suite, not here.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.dispatch_outcome import (
    DispatchOutcome,
    classify_dispatch_exception,
)

# --------------------------------------------------------------- helpers


class _FakeResponse:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _exc_with(
    *,
    name: str = "SomeError",
    status_code: int | None = None,
    message: str | None = None,
    retry_after: str | None = None,
) -> Exception:
    """Build an exception that mimics LiteLLM/httpx's attribute
    surface (status_code, message, response.headers)."""
    exc = type(name, (Exception,), {})(message or name)
    if status_code is not None:
        exc.status_code = status_code  # type: ignore[attr-defined]
    if message is not None:
        exc.message = message  # type: ignore[attr-defined]
    if retry_after is not None:
        exc.response = _FakeResponse({"retry-after": retry_after})  # type: ignore[attr-defined]
    return exc


# --------------------------------------------------------------- classification


def test_litellm_timeout_class_is_upstream_silent() -> None:
    """A ``Timeout``-named exception → upstream_silent, the
    Phase 4.9 contract."""
    exc = _exc_with(name="Timeout", message="timed out")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_silent"
    assert out.error_class == "Timeout"
    assert "timed out" in out.error_detail


def test_408_with_timeout_message_is_upstream_silent() -> None:
    exc = _exc_with(name="APIError", status_code=408, message="Request timeout occurred")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_silent"


def test_429_is_rate_limited_with_retry_after() -> None:
    exc = _exc_with(name="RateLimitError", status_code=429, message="slow down", retry_after="12")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_rate_limited"
    assert out.upstream_status == 429
    assert out.retry_after == "12"


def test_429_without_header_synthesizes_retry_after() -> None:
    exc = _exc_with(name="RateLimitError", status_code=429, message="slow down")
    out = classify_dispatch_exception(exc)
    assert out.retry_after == "5"


def test_529_synthesizes_thirty_second_retry_after() -> None:
    exc = _exc_with(name="OverloadedError", status_code=529, message="overloaded")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_rate_limited"
    assert out.upstream_status == 529
    assert out.retry_after == "30"


def test_401_is_client_error() -> None:
    exc = _exc_with(name="AuthenticationError", status_code=401, message="bad key")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_client_error"
    assert out.upstream_status == 401


def test_400_is_client_error() -> None:
    exc = _exc_with(name="BadRequestError", status_code=400, message="malformed")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_client_error"


def test_500_is_server_error() -> None:
    exc = _exc_with(name="InternalServerError", status_code=500, message="boom")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_server_error"
    assert out.upstream_status == 500


def test_connection_error_with_no_status_is_server_error() -> None:
    """Transport failures (ConnectError, ReadTimeout) don't
    expose a status_code → fall through to the catch-all."""
    exc = _exc_with(name="ConnectError", message="Name or service not known")
    out = classify_dispatch_exception(exc)
    assert out.status == "upstream_server_error"
    assert out.upstream_status is None
    assert out.error_class == "ConnectError"


def test_classifier_never_returns_unreachable() -> None:
    """``unreachable`` is the probe's refinement after a
    reachability ping; the shared classifier never emits it."""
    for exc in (
        _exc_with(name="ConnectError", message="refused"),
        _exc_with(name="Timeout", message="timed out"),
        _exc_with(name="RateLimitError", status_code=429),
        _exc_with(name="AuthenticationError", status_code=401),
    ):
        assert classify_dispatch_exception(exc).status != "unreachable"


def test_error_detail_truncated_at_500() -> None:
    exc = _exc_with(name="BigError", status_code=500, message="x" * 2000)
    out = classify_dispatch_exception(exc)
    assert len(out.error_detail) == 500


# --------------------------------------------------------------- to_log_fields


def test_to_log_fields_matches_legacy_shape() -> None:
    """The dict shape the chat path's log_request consumes:
    error_type (not status), error_class, error_detail, plus
    optional upstream_status / retry_after only when set."""
    out = DispatchOutcome(
        status="upstream_rate_limited",
        error_class="RateLimitError",
        error_detail="slow down",
        upstream_status=429,
        retry_after="5",
    )
    fields = out.to_log_fields()
    assert fields == {
        "error_type": "upstream_rate_limited",
        "error_class": "RateLimitError",
        "error_detail": "slow down",
        "upstream_status": 429,
        "retry_after": "5",
    }


def test_to_log_fields_omits_unset_optionals() -> None:
    out = DispatchOutcome(
        status="upstream_silent",
        error_class="Timeout",
        error_detail="timed out",
    )
    fields = out.to_log_fields()
    assert fields == {
        "error_type": "upstream_silent",
        "error_class": "Timeout",
        "error_detail": "timed out",
    }
    assert "upstream_status" not in fields
    assert "retry_after" not in fields


# --------------------------------------------------------------- property: totality


_VALID_STATUSES = {
    "upstream_silent",
    "upstream_rate_limited",
    "upstream_client_error",
    "upstream_server_error",
    "unreachable",
}


# Phase 7.6, Property 1: Failure classification is total
@given(
    name=st.sampled_from(
        [
            "Timeout",
            "RateLimitError",
            "AuthenticationError",
            "BadRequestError",
            "InternalServerError",
            "ConnectError",
            "ReadTimeout",
            "APIConnectionError",
            "ServiceUnavailableError",
            "ValueError",
            "RuntimeError",
        ]
    ),
    status_code=st.one_of(
        st.none(),
        st.sampled_from([400, 401, 403, 408, 422, 429, 500, 502, 503, 529]),
    ),
    message=st.text(min_size=0, max_size=50),
)
@settings(max_examples=200, deadline=None)
def test_property_classification_is_total(name: str, status_code: int | None, message: str) -> None:
    """For any exception shape, the classifier returns exactly
    one valid status and never raises."""
    exc = _exc_with(name=name, status_code=status_code, message=message or None)
    out = classify_dispatch_exception(exc)
    assert out.status in _VALID_STATUSES
    # The shared classifier specifically never emits unreachable.
    assert out.status != "unreachable"
    assert isinstance(out.error_class, str) and out.error_class
