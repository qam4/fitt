"""Tests for the dashboard edit substrate (F10).

Covers three concerns, each in its own section:

* CSRF — token issuance, signature mismatch, expiry,
  bearer-only-bypass.
* Optimistic-mtime — successful save, mtime conflict,
  creation race, validation rejection, IO failure.
* Audit-on-edit — every attempt writes one entry; success
  records ``ok=True`` with bytes-written delta, every
  failure mode records ``ok=False`` with a structured
  reason in ``extra``.

The substrate ships without UI surfaces (F11 onwards is when
real edit views land). These tests therefore exercise the
helpers directly with synthetic Request objects and a real
on-disk audit log.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.audit import AuditLog
from gateway.dashboard.auth import COOKIE_NAME, DashboardAuth
from gateway.dashboard.edit import (
    CsrfMismatch,
    EditError,
    MtimeConflict,
    ValidationFailed,
    csrf_required,
    issue_csrf,
    save_file_with_mtime,
    verify_csrf,
)

# --------------------------------------------------------------- helpers


def _make_auth(tmp_path: Path) -> DashboardAuth:
    return DashboardAuth(
        allowed_bearers=[("token-aaa", "ide"), ("token-bbb", None)],
        key_path=tmp_path / "dashboard.key",
    )


def _make_request(
    *,
    cookie_value: str | None = None,
    auth: DashboardAuth | None = None,
) -> MagicMock:
    """Synthesise just enough of a Starlette Request for the
    edit helpers. Real Request objects need a full ASGI scope;
    a Mock with the two attributes the helpers touch is
    cleaner."""
    cookies = {}
    if cookie_value is not None:
        cookies[COOKIE_NAME] = cookie_value

    state = MagicMock()
    state.dashboard_auth = auth

    request = MagicMock()
    request.cookies = cookies
    request.app.state = state
    return request


def _make_audit_log(tmp_path: Path) -> AuditLog:
    return AuditLog(
        path=tmp_path / "audit.jsonl",
        key_path=tmp_path / "audit.key",
    )


# --------------------------------------------------------------- CSRF


def test_csrf_round_trip(tmp_path: Path) -> None:
    auth = _make_auth(tmp_path)
    request = _make_request(cookie_value="cookie-xyz", auth=auth)
    token = issue_csrf(request, key=auth.key())
    assert verify_csrf(token, request, key=auth.key()) is True


def test_csrf_rejects_wrong_cookie(tmp_path: Path) -> None:
    """A token issued for cookie A must not validate when
    submitted with cookie B."""
    auth = _make_auth(tmp_path)
    req_a = _make_request(cookie_value="cookie-aaa", auth=auth)
    req_b = _make_request(cookie_value="cookie-bbb", auth=auth)
    token = issue_csrf(req_a, key=auth.key())
    assert verify_csrf(token, req_b, key=auth.key()) is False


def test_csrf_rejects_signature_mismatch(tmp_path: Path) -> None:
    auth_a = _make_auth(tmp_path / "a")
    auth_b = _make_auth(tmp_path / "b")
    request = _make_request(cookie_value="cookie-xyz", auth=auth_a)
    token = issue_csrf(request, key=auth_a.key())
    assert verify_csrf(token, request, key=auth_b.key()) is False


def test_csrf_rejects_expired_token(tmp_path: Path) -> None:
    """An expired token (expires_at in the past) fails
    verification."""
    auth = _make_auth(tmp_path)
    request = _make_request(cookie_value="cookie-xyz", auth=auth)
    # Hand-craft a token with expires_at in the past.
    import base64
    import hashlib
    import hmac as _hmac

    nonce = "expired-nonce"
    expires_at = int(time.time() - 10)
    msg = f"cookie-xyz|{nonce}|{expires_at}".encode()
    sig = _hmac.new(auth.key(), msg, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    token = f"{nonce}.{expires_at}.{sig_b64}"

    assert verify_csrf(token, request, key=auth.key()) is False


@pytest.mark.parametrize(
    "garbage",
    ["", "no-dots", "a.b", "a.b.c.d", "abc.notanumber.def"],
)
def test_csrf_rejects_malformed(garbage: str, tmp_path: Path) -> None:
    auth = _make_auth(tmp_path)
    request = _make_request(cookie_value="cookie-xyz", auth=auth)
    assert verify_csrf(garbage, request, key=auth.key()) is False


def test_csrf_required_passes_for_bearer_only(tmp_path: Path) -> None:
    """Bearer-only requests (no dashboard cookie) skip CSRF.
    The bearer is the cross-site protection."""
    auth = _make_auth(tmp_path)
    request = _make_request(cookie_value=None, auth=auth)
    # No CSRF submitted; should NOT raise.
    csrf_required(request, "")


def test_csrf_required_raises_for_cookie_with_bad_token(tmp_path: Path) -> None:
    auth = _make_auth(tmp_path)
    request = _make_request(cookie_value="cookie-xyz", auth=auth)
    with pytest.raises(CsrfMismatch):
        csrf_required(request, "garbage")


def test_csrf_required_raises_when_auth_missing(tmp_path: Path) -> None:
    """If the dashboard auth context isn't wired, treat as
    no-go regardless of token contents."""
    request = _make_request(cookie_value="cookie-xyz", auth=None)
    with pytest.raises(CsrfMismatch):
        csrf_required(request, "ignored")


# --------------------------------------------------------------- save / mtime


def test_save_writes_new_file(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "new.md"
    result = save_file_with_mtime(
        path=target,
        new_content="hello\n",
        expected_mtime=None,
        audit_log=audit,
        client="ide",
    )
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert result.bytes_written == 6
    assert result.bytes_changed_delta == 6


def test_save_overwrites_existing_file(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "existing.md"
    target.write_bytes(b"old\n")
    expected = target.stat().st_mtime
    result = save_file_with_mtime(
        path=target,
        new_content="new content\n",
        expected_mtime=expected,
        audit_log=audit,
        client="ide",
    )
    assert target.read_text(encoding="utf-8") == "new content\n"
    assert result.bytes_changed_delta == len("new content\n") - 4


def test_save_rejects_mtime_conflict(tmp_path: Path) -> None:
    """Render reads mtime A; another writer touches the file;
    save submits A → reject with the current mtime exposed."""
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "raced.md"
    target.write_text("first\n", encoding="utf-8")
    stale_mtime = target.stat().st_mtime - 100  # pretend we read it earlier

    with pytest.raises(MtimeConflict) as exc_info:
        save_file_with_mtime(
            path=target,
            new_content="second\n",
            expected_mtime=stale_mtime,
            audit_log=audit,
            client="ide",
        )
    assert exc_info.value.current_mtime > 0
    # The on-disk content must be unchanged.
    assert target.read_text(encoding="utf-8") == "first\n"


def test_save_rejects_creation_race(tmp_path: Path) -> None:
    """Render saw "no file"; meanwhile someone created it.
    Save with expected_mtime=None must NOT silently overwrite."""
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "appeared.md"
    target.write_text("appeared\n", encoding="utf-8")

    with pytest.raises(MtimeConflict):
        save_file_with_mtime(
            path=target,
            new_content="overwrite\n",
            expected_mtime=None,
            audit_log=audit,
            client="ide",
        )
    # Content unchanged.
    assert target.read_text(encoding="utf-8") == "appeared\n"


def test_save_runs_validator(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "validated.md"

    def validator(content: str) -> str | None:
        if "forbidden" in content:
            return "the word 'forbidden' is not allowed"
        return None

    with pytest.raises(ValidationFailed) as exc_info:
        save_file_with_mtime(
            path=target,
            new_content="this has a forbidden word\n",
            expected_mtime=None,
            audit_log=audit,
            client="ide",
            validate=validator,
        )
    assert "forbidden" in exc_info.value.detail
    assert not target.exists()


def test_save_validator_pass_writes_file(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "ok.md"

    def validator(content: str) -> str | None:
        return None  # always pass

    save_file_with_mtime(
        path=target,
        new_content="ok\n",
        expected_mtime=None,
        audit_log=audit,
        client="ide",
        validate=validator,
    )
    assert target.read_text(encoding="utf-8") == "ok\n"


def test_save_atomic_no_partial_on_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate an IO failure mid-rename. The destination
    must still hold its previous content, and no .tmp file
    is left behind in the canonical name."""
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "atomic.md"
    target.write_text("preserved\n", encoding="utf-8")
    expected = target.stat().st_mtime

    def fake_replace(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated rename failure")

    monkeypatch.setattr("os.replace", fake_replace)

    with pytest.raises(EditError):
        save_file_with_mtime(
            path=target,
            new_content="should not land\n",
            expected_mtime=expected,
            audit_log=audit,
            client="ide",
        )

    # The existing content survives.
    assert target.read_text(encoding="utf-8") == "preserved\n"


# --------------------------------------------------------------- audit chain


def _read_audit_entries(audit_log: AuditLog) -> list[dict]:
    return audit_log.iter_entries()


def test_save_emits_one_audit_entry_per_success(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "audited.md"
    save_file_with_mtime(
        path=target,
        new_content="content\n",
        expected_mtime=None,
        audit_log=audit,
        client="cli",
    )
    entries = _read_audit_entries(audit)
    assert len(entries) == 1
    e = entries[0]
    assert e["tool"] == "dashboard.edit"
    assert e["client"] == "cli"
    assert e["ok"] is True
    assert e["decision"] == "approved"
    # Path lands in args + extra so audit-tail filters work.
    assert e["args"]["path"] == str(target)
    assert e["extra"]["bytes_written"] == len(b"content\n")


def test_save_emits_audit_entry_on_mtime_conflict(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "conflict.md"
    target.write_text("first\n", encoding="utf-8")

    with pytest.raises(MtimeConflict):
        save_file_with_mtime(
            path=target,
            new_content="second\n",
            expected_mtime=target.stat().st_mtime - 100,
            audit_log=audit,
            client="ide",
        )

    entries = _read_audit_entries(audit)
    assert len(entries) == 1
    e = entries[0]
    assert e["ok"] is False
    assert e["decision"] == "rejected"
    assert e["extra"]["reason"] == "mtime_conflict"


def test_save_emits_audit_entry_on_validation_failure(tmp_path: Path) -> None:
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "validated.md"

    def validator(_: str) -> str | None:
        return "always rejects"

    with pytest.raises(ValidationFailed):
        save_file_with_mtime(
            path=target,
            new_content="anything\n",
            expected_mtime=None,
            audit_log=audit,
            client="webui",
            validate=validator,
        )

    entries = _read_audit_entries(audit)
    assert len(entries) == 1
    e = entries[0]
    assert e["ok"] is False
    assert e["decision"] == "rejected"
    assert e["extra"]["reason"] == "validation_failed"
    assert "always rejects" in e["error"]


def test_save_chain_grows_across_multiple_attempts(tmp_path: Path) -> None:
    """Each save (success or failure) appends one entry; the
    HMAC chain remains valid across the lot."""
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "many.md"

    save_file_with_mtime(
        path=target,
        new_content="v1\n",
        expected_mtime=None,
        audit_log=audit,
        client="ide",
    )
    expected_mtime = target.stat().st_mtime
    # Force a meaningful mtime delta on filesystems with coarse
    # resolution (Windows NTFS rounds to ~10ms; some FAT
    # variants are 2s). Without this, v1 and v2 can land at
    # the same mtime, making the deliberate-stale-mtime check
    # below pass when it shouldn't.
    time.sleep(0.05)
    save_file_with_mtime(
        path=target,
        new_content="v2\n",
        expected_mtime=expected_mtime,
        audit_log=audit,
        client="ide",
    )
    # Now a deliberate conflict.
    with pytest.raises(MtimeConflict):
        save_file_with_mtime(
            path=target,
            new_content="v3-races\n",
            expected_mtime=expected_mtime,  # stale on purpose
            audit_log=audit,
            client="ide",
        )

    entries = _read_audit_entries(audit)
    assert len(entries) == 3
    # Verify the chain is internally consistent.
    verify_result = audit.verify()
    assert verify_result.ok, verify_result.reason


def test_save_audit_log_failure_does_not_break_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken audit log (e.g. unwritable key) must not
    prevent a successful save. The save is the source of
    truth; audit is best-effort."""
    audit = _make_audit_log(tmp_path)
    target = tmp_path / "audit_broken.md"

    # Force the audit append to raise.
    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("audit disk full")

    monkeypatch.setattr(audit, "append", boom)

    # Save still completes.
    save_file_with_mtime(
        path=target,
        new_content="content\n",
        expected_mtime=None,
        audit_log=audit,
        client="ide",
    )
    assert target.read_text(encoding="utf-8") == "content\n"


def test_save_with_no_audit_log_works(tmp_path: Path) -> None:
    """Tests pass ``audit_log=None`` to skip audit; the save
    still works."""
    target = tmp_path / "no_audit.md"
    save_file_with_mtime(
        path=target,
        new_content="content\n",
        expected_mtime=None,
        audit_log=None,
        client="ide",
    )
    assert target.read_text(encoding="utf-8") == "content\n"
