"""Dashboard edit substrate (F10 in the Phase 7 followups list).

Foundation for every later edit-capable view (F11 identity +
lessons; F12 projects + cron; F13 skills; F14 config; F15
secrets). No UI surfaces in this commit — just the three
load-bearing pieces an edit form needs:

1. **CSRF tokens** signed against the dashboard's session
   cookie. Issued at form-render time, validated at
   form-submit time. Bearer-token requests are exempt
   (they're tools, not browsers; the bearer itself is the
   anti-CSRF).

2. **Optimistic-mtime concurrency.** Every editable file's
   render packs a "loaded at" hint into the form. The save
   handler reads the file's current mtime, compares against
   the hint, and rejects if disk moved underneath. Cheaper
   than locking, fits FITT's "files are the source of truth"
   posture, and handles the ``learn_*`` tool collision case
   that motivated this in the first place.

3. **Audit-on-edit.** Every successful save writes one
   :class:`gateway.audit.AuditEntry` into the existing
   chain with ``tool="dashboard.edit"`` and an ``extra``
   payload naming the file path, the operator's client tag,
   the bytes-changed delta. Failures (CSRF mismatch, mtime
   conflict, validation error) audit too, with ``ok=False``,
   so the trail captures denied edits the same way it
   captures denied tool calls.

The substrate also includes :func:`save_file_with_mtime`, an
atomic write helper that combines mtime check + tmp-file +
rename + audit emission. Edit views call this and never
write files directly so the audit + concurrency story stays
in one place.

Failure semantics
-----------------

* Bad CSRF → 403 with a ``code: csrf_mismatch`` envelope.
  The operator sees a "form expired, reload and retry"
  banner; the audit log gets the denied attempt.
* Mtime conflict → 409 with the operator's submitted bytes
  preserved in the response so they can copy-paste back
  after they see what changed. Audit logs ``ok=False`` with
  the disk-side mtime in ``extra.detail``.
* Validation failure → 400 with the validator's error in
  the body. Audit logs ``ok=False`` with the first error.
* IO failure → 500 with no leaked path/error to the
  operator beyond a generic message; the audit log captures
  full detail.

The substrate is **opt-in per call site**: an edit view
calls into this module rather than hooking into a global
middleware. That keeps F11-F15 explicit about which routes
mutate state and removes the surprise factor of a
middleware that quietly swallows POSTs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from ..audit import AuditLog, new_entry
from .auth import COOKIE_NAME, DashboardAuth

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- exceptions


class EditError(Exception):
    """Base for dashboard-edit failures.

    Each subclass maps to a specific HTTP status; the route
    handler renders the right shape. Carrying the structured
    detail (current_mtime for conflicts, validator errors)
    on the exception keeps the route handlers thin — they
    catch one type, render the right thing.
    """

    http_status: int = 500
    code: str = "edit_error"


class CsrfMismatch(EditError):
    """Submitted CSRF token didn't sign-match the session cookie."""

    http_status = 403
    code = "csrf_mismatch"


class MtimeConflict(EditError):
    """File's on-disk mtime moved between render and submit.

    Carries ``current_mtime`` so the route can show "the
    file changed at <ts>; here's the new content" alongside
    the operator's submission."""

    http_status = 409
    code = "mtime_conflict"

    def __init__(self, message: str, *, current_mtime: float) -> None:
        super().__init__(message)
        self.current_mtime = current_mtime


class ValidationFailed(EditError):
    """Save handler's validator rejected the new content.

    The route renders ``detail`` next to the operator's
    submission. ``detail`` is operator-readable; the
    validator's job is to say "what's wrong" in plain
    language."""

    http_status = 400
    code = "validation_failed"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------- CSRF


_CSRF_TOKEN_TTL_S = 3600.0
"""How long a CSRF token stays valid. One hour is plenty for
"render the form, the operator types, submits"; expiring sooner
than that produces nuisance reloads, longer makes a stolen
cookie's window of usefulness wider than it has to be."""


def _cookie_signature(request: Request) -> str:
    """Return a stable string derived from the dashboard cookie.

    The CSRF token is signed against this so a token can only
    be used by the holder of the cookie that produced it. We
    use the cookie value itself as the binding (rather than
    the cookie's signature half) so cookies issued at different
    times can't be reused interchangeably.

    Returns ``""`` when no cookie is present — the
    caller treats bearer-only requests differently."""
    return request.cookies.get(COOKIE_NAME, "")


def issue_csrf(request: Request, *, key: bytes) -> str:
    """Mint a CSRF token for the current request.

    Token shape: ``<nonce>.<expires_at>.<sig>``. Nonce
    randomises per call so the same form rendered twice
    produces different tokens (prevents a leaked HTML
    snapshot from reusing a stale token). ``expires_at``
    bounds the lifetime. ``sig`` is HMAC-SHA256 over
    ``b"<cookie>|<nonce>|<expires_at>"`` so the token can
    only be used by the cookie's holder.
    """
    nonce = uuid.uuid4().hex[:16]
    expires_at = int(time.time() + _CSRF_TOKEN_TTL_S)
    cookie = _cookie_signature(request)
    msg = f"{cookie}|{nonce}|{expires_at}".encode()
    sig = hmac.new(key, msg, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{nonce}.{expires_at}.{sig_b64}"


def verify_csrf(token: str, request: Request, *, key: bytes) -> bool:
    """Return True iff ``token`` is a valid CSRF for the
    current request's cookie. False on every failure mode
    (malformed token, signature mismatch, expired) — caller
    raises :class:`CsrfMismatch` on False without leaking which
    bit was wrong.

    Bearer-only requests (no cookie present) intentionally
    fail this check: a tool authenticated by bearer doesn't
    need CSRF in the first place, but if it submits a CSRF
    field it still has to be a real one. The route handler is
    responsible for the bearer-exempt branch."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, expires_str, sig_b64 = parts
    try:
        expires_at = int(expires_str)
    except ValueError:
        return False
    if time.time() >= expires_at:
        return False

    cookie = _cookie_signature(request)
    msg = f"{cookie}|{nonce}|{expires_at}".encode()
    expected = hmac.new(key, msg, hashlib.sha256).digest()
    try:
        provided = base64.urlsafe_b64decode(_pad(sig_b64))
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(expected, provided)


def _pad(b64: str) -> str:
    """Re-pad URL-safe base64 to a multiple of 4."""
    return b64 + "=" * (-len(b64) % 4)


def csrf_required(request: Request, submitted: str) -> None:
    """Raise :class:`CsrfMismatch` when the request needs CSRF
    but ``submitted`` doesn't match.

    Bearer-only requests (no dashboard cookie) skip the check —
    the bearer is the anti-CSRF for those callers. Cookie-bearing
    requests must present a valid token.

    Used by route handlers as the first action on POST/PUT/DELETE
    for the dashboard edit surface.
    """
    cookie = _cookie_signature(request)
    if not cookie:
        # Bearer-only path. The bearer itself is the
        # cross-site protection: a cross-site request can't
        # set Authorization headers without explicit CORS
        # cooperation, which we don't grant.
        return
    auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
    if auth is None:
        # The dashboard isn't wired into this app. Treat as
        # no-go — same posture as authorize_request().
        raise CsrfMismatch("dashboard auth not configured")
    if not verify_csrf(submitted, request, key=auth.key()):
        raise CsrfMismatch("CSRF token did not match the current session")


# --------------------------------------------------------------- file save


@dataclass(frozen=True, slots=True)
class FileSaveResult:
    """What a successful :func:`save_file_with_mtime` call
    returns. Useful for the route handler's success-path
    redirect ("saved at <ts>")."""

    path: Path
    new_mtime: float
    bytes_written: int
    bytes_changed_delta: int


def _read_mtime(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def save_file_with_mtime(
    *,
    path: Path,
    new_content: str,
    expected_mtime: float | None,
    audit_log: AuditLog | None,
    client: str,
    session_key: str = "main",
    validate: Callable[[str], str | None] | None = None,
) -> FileSaveResult:
    """Atomically save ``new_content`` to ``path`` if the disk
    mtime still matches ``expected_mtime``.

    The mtime check is the optimistic-concurrency floor: the
    edit view rendered the file at ``expected_mtime``; if any
    concurrent writer (operator on the CLI, ``learn_*`` tool,
    another browser tab) has touched the file since, this
    save refuses with :class:`MtimeConflict`. The route
    handler renders the conflict so the operator can re-load
    and reconcile.

    ``expected_mtime=None`` means "the file didn't exist when
    we rendered the form." A creation race (file appeared
    underneath us) raises ``MtimeConflict`` with the
    current mtime; the operator decides whether to overwrite.

    ``validate`` is an optional callable that runs against
    the proposed bytes before write. It returns ``None`` on
    pass, an operator-readable error string on fail. The
    string lands in the :class:`ValidationFailed` exception
    and in the audit entry's ``error`` field.

    Atomicity: write to ``<path>.<rand>.tmp`` then rename. If
    the rename fails the tmp file gets unlinked. POSIX rename
    is atomic; Windows ``os.replace`` matches POSIX semantics
    at the API level. The destination's previous content is
    never partial-written.

    Audit emission: one entry per attempt — successes with
    ``ok=True``, every failure mode (mtime conflict, validation,
    IO) with ``ok=False`` and the reason in ``error``. The
    audit log can be ``None`` for tests; production always
    passes the live one.
    """
    started_at = time.time()
    current_mtime = _read_mtime(path)
    bytes_old = 0
    if path.exists():
        try:
            bytes_old = path.stat().st_size
        except OSError:
            bytes_old = 0

    # Mtime mismatch handling. The two None/non-None cases
    # are both "file changed underneath us" from the
    # caller's perspective — surface them with the same
    # exception so route handlers stay simple.
    expected_eq_actual = (expected_mtime is None and current_mtime is None) or (
        expected_mtime is not None
        and current_mtime is not None
        and abs(expected_mtime - current_mtime) < 1e-6
    )
    if not expected_eq_actual:
        _audit_edit(
            audit_log,
            client=client,
            session_key=session_key,
            path=path,
            ok=False,
            decision="rejected",
            error=f"mtime conflict (expected {expected_mtime}, on disk {current_mtime})",
            duration_ms=int((time.time() - started_at) * 1000),
            extra={"reason": "mtime_conflict", "current_mtime": current_mtime},
        )
        raise MtimeConflict(
            "file changed on disk between render and save",
            current_mtime=current_mtime if current_mtime is not None else 0.0,
        )

    if validate is not None:
        verdict = validate(new_content)
        if verdict is not None:
            _audit_edit(
                audit_log,
                client=client,
                session_key=session_key,
                path=path,
                ok=False,
                decision="rejected",
                error=str(verdict),
                duration_ms=int((time.time() - started_at) * 1000),
                extra={"reason": "validation_failed"},
            )
            raise ValidationFailed(str(verdict))

    # Atomic write.
    encoded = new_content.encode("utf-8")
    tmp_suffix = uuid.uuid4().hex[:8]
    tmp = path.with_suffix(path.suffix + f".{tmp_suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(encoded)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        _audit_edit(
            audit_log,
            client=client,
            session_key=session_key,
            path=path,
            ok=False,
            decision="error",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started_at) * 1000),
            extra={"reason": "io_error"},
        )
        raise EditError(f"write failed: {type(exc).__name__}") from exc

    new_mtime = _read_mtime(path) or time.time()
    bytes_written = len(encoded)
    delta = bytes_written - bytes_old

    _audit_edit(
        audit_log,
        client=client,
        session_key=session_key,
        path=path,
        ok=True,
        decision="approved",
        error="",
        duration_ms=int((time.time() - started_at) * 1000),
        extra={
            "bytes_written": bytes_written,
            "bytes_old": bytes_old,
            "bytes_changed_delta": delta,
        },
    )

    return FileSaveResult(
        path=path,
        new_mtime=new_mtime,
        bytes_written=bytes_written,
        bytes_changed_delta=delta,
    )


def _audit_edit(
    audit_log: AuditLog | None,
    *,
    client: str,
    session_key: str,
    path: Path,
    ok: bool,
    decision: str,
    error: str,
    duration_ms: int,
    extra: dict[str, Any],
) -> None:
    """Append one audit entry for an edit attempt.

    Tool name is fixed: ``dashboard.edit``. Args carry the
    relative-or-absolute path under ``$FITT_HOME`` (sensitive
    paths get redacted by the audit layer's existing
    redaction rules). The ``extra`` block carries the
    bookkeeping (bytes written, mtime, validation reason) for
    grep-able forensics later.
    """
    if audit_log is None:
        return
    full_extra = {"path": str(path), **extra}
    try:
        audit_log.append(
            new_entry(
                session_key=session_key,
                client=client,
                tool="dashboard.edit",
                args={"path": str(path)},
                decision=decision,
                ok=ok,
                duration_ms=duration_ms,
                error=error,
                extra=full_extra,
            )
        )
    except Exception as exc:
        # Audit failure must not break the save: the on-disk
        # action is the source of truth, and refusing the save
        # because audit broke would be worse than auditing
        # the audit failure. Log and move on.
        _log.warning(
            "dashboard.audit_append_failed",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
