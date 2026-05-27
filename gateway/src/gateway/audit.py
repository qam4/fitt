"""Append-only audit log with HMAC chain.

Every tool call flows through here. The log is the single
trustworthy record of "what did FITT do, at whose request, and
what was the outcome?" — the counterpart to the approval
middleware, which decides *whether* to run things, while audit
records *what actually ran*.

Design points
-------------

* **Append-only, JSONL on disk** at ``$FITT_HOME/audit.jsonl``.
  Easy to inspect by hand, trivial to grep, no database.
* **HMAC chain.** Each entry carries a ``prev_hmac`` pointing at
  the previous entry's ``hmac``. The HMAC is computed over the
  JSON-serialised entry (minus the ``hmac`` field itself) keyed
  with a secret at ``$FITT_HOME/audit.key``. Tamper with any
  entry — change a field, insert, delete, reorder — and
  :meth:`verify` catches it.
* **Key lifecycle.** The HMAC key is generated on first write
  with 0600 perms (where supported; Windows uses the default
  user ACL). Never rotated. If the key file goes missing, a new
  one is generated and the existing chain becomes unverifiable
  from that point — which is the right behaviour: a missing key
  is indistinguishable from tampering, so we refuse to extend
  the old chain silently.
* **Redaction before writing.** Args that look like secrets
  (keys matching ``token``, ``password``, ``api_key``, ...; values
  that look like an API key) are replaced with ``"<redacted>"``
  before the entry is hashed. The hash is therefore stable
  across "what did the operator see" and "what's on disk".

The audit layer is **synchronous** and **file-locked**. Audit
lives off the hot path — the tool has already executed — so the
~1 ms cost of an fsync-serialised write is invisible. Sync
writes also mean a crash can't leave a half-written chain entry.

Concurrent writes from a single gateway process are serialised
by ``self._lock``. Multiple gateway processes writing the same
file is out of scope (there's only one gateway per hub).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from time import time
from typing import Any

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- redaction


_SECRET_KEY_NAMES = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|bearer|"
    r"credential|auth)",
)
"""Argument-key patterns treated as secrets. Matching keys have
their values replaced with ``<redacted>`` in the audit entry."""

_SECRET_VALUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI-style
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
    re.compile(r"sk-or-[vV][0-9]+-[A-Za-z0-9_-]{20,}"),  # OpenRouter
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub
    re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),  # Slack bot
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    # Long URL-safe base64 strings that look like keys.
    # Conservative threshold + restriction to ``[A-Za-z0-9_-]``
    # so we don't redact file hashes, UUIDs, or the path
    # components that show up in pytest tmp paths on Linux
    # (``/tmp/pytest-of-runner/.../<long-test-name>/audited.md``
    # used to round-trip as ``/<redacted>.md`` because ``/``
    # was in the character class — every modern key format
    # above uses URL-safe base64, so dropping ``/`` and ``+``
    # loses no real coverage). Padding-equals tolerated for
    # the rare legacy base64 case.
    re.compile(r"\b[A-Za-z0-9_-]{40,}={0,2}\b"),
]
"""Value-shape patterns that look like secrets regardless of the
key name. Applied to every string value before writing."""


_REDACTED = "<redacted>"


def redact(value: Any) -> Any:
    """Walk a JSON-ish structure and replace secret-looking pieces
    with ``<redacted>``.

    Recurses through dicts and lists; leaves numbers, booleans,
    None, and short strings alone. The rules are *conservative* —
    we'd rather leak a value shaped like a UUID than redact a
    file path — because audit entries are supposed to be useful
    for debugging. When in doubt, write a test case rather than
    broadening a regex.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_NAMES.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        s = value
        # Named patterns (sk-, ghp_, JWT, ...) always fire — they
        # catch keys embedded in larger strings (URL with embedded
        # token, log line with a key in it).
        for pat in _SECRET_VALUE_PATTERNS[:-1]:
            s = pat.sub(_REDACTED, s)
        # The catch-all "long URL-safe base64" pattern only fires
        # when the string has no path separator. Structured paths
        # (Linux pytest tmp dirs like
        # ``/tmp/pytest-of-runner/.../<long-test-name>/file.md``,
        # Windows ``C:\Users\...\AppData\...\file.md``, URLs)
        # used to round-trip through the redactor mangled because
        # a path component happened to be 40+ chars of word-class.
        # Modern key formats (sk-, sk-ant-, sk-or-, ghp_, JWT, ...)
        # do not contain ``/`` or ``\`` in their value shapes, so
        # excluding strings that contain either separator loses
        # no real coverage on the catch-all rung — and the named
        # patterns above still catch keys embedded inside URLs.
        if "/" not in s and "\\" not in s:
            s = _SECRET_VALUE_PATTERNS[-1].sub(_REDACTED, s)
        return s
    return value


# --------------------------------------------------------------- entry


@dataclass
class AuditEntry:
    """One audit-log record.

    The canonical shape written to disk. ``prev_hmac`` and ``hmac``
    are populated by :class:`AuditLog.append` — callers construct
    everything else. Kept as an ordinary (mutable) dataclass so
    the log can fill in the chain fields without dataclasses
    acrobatics; each entry is written exactly once and immediately
    forgotten by the log."""

    ts: float
    """Unix epoch seconds. Callers pass their own clock in tests."""

    session_key: str
    """Which session initiated the call. ``"main"`` by default."""

    client: str
    """The originating client tag (``ide``, ``telegram``, ``webui``,
    ``cli``). Resolved earlier by the auth middleware."""

    tool: str
    """Tool name (e.g. ``read_file``, ``mcp.slack.send_message``)."""

    args: dict[str, Any]
    """Arguments the tool was invoked with. Redacted before
    hashing; stored redacted on disk."""

    decision: str
    """The approval decision reason:
    ``auto`` / ``approved`` / ``trust_session`` / ``yolo`` /
    ``rejected`` / ``timeout`` / ``blocked`` / ``denied_deny_list``.
    Matches the ``reason`` field on ``ApprovalDecision``."""

    ok: bool
    """Whether the tool ran successfully. ``False`` for decisions
    that short-circuited before dispatch *and* for decisions that
    allowed dispatch but the tool itself returned an error."""

    duration_ms: int
    """Wall-clock milliseconds from "entered dispatch" to "got
    result". ``0`` when the call was short-circuited by the
    approval/deny layers."""

    error: str = ""
    """Error message when the tool failed; empty on success."""

    prev_hmac: str = ""
    """Hex digest of the previous entry's ``hmac``. ``""`` for the
    very first entry in the log."""

    hmac: str = ""
    """Hex digest of this entry's chain-protected fields."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Free-form optional metadata: the pattern label for a
    ``denied_deny_list`` decision, the approval id for an
    ``approved`` decision, the backend tag for model dispatch. Kept
    off the named fields so the entry schema doesn't thrash with
    every new subsystem, but still chained into the HMAC so
    tampering is still caught."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of :meth:`AuditLog.verify`.

    ``ok`` is True when every entry's HMAC chains correctly from
    the previous. ``bad_line`` is the 1-based line number of the
    first failure; 0 when the file is empty or verifying succeeds.
    ``reason`` is a short human string for CLI output."""

    ok: bool
    total_lines: int
    bad_line: int = 0
    reason: str = ""


# --------------------------------------------------------------- log


_KEY_BYTES = 32
"""HMAC key length. 32 bytes is comfortable for SHA-256, which
produces 32-byte digests, and matches ``secrets.token_bytes(32)``
in the rest of the codebase."""


class AuditLog:
    """Append-only JSONL log with HMAC-chained entries.

    One instance per gateway process; stateful (remembers the
    last ``hmac`` so :meth:`append` can chain). Construction is
    cheap — the key file is read or generated lazily on first
    write, so tests that never call :meth:`append` don't need a
    writable filesystem.
    """

    def __init__(self, path: Path, key_path: Path) -> None:
        self._path = path
        self._key_path = key_path
        self._lock = threading.Lock()
        self._key: bytes | None = None
        # Tail the file once on startup to find the last chained
        # HMAC, so a gateway restart doesn't reset ``prev_hmac``
        # to "" and break the chain.
        self._last_hmac: str = _tail_last_hmac(path)

    # ------------------------------------------------------------------ public

    def append(self, entry: AuditEntry) -> AuditEntry:
        """Append ``entry`` to the log. Fills in ``prev_hmac``,
        ``hmac``, and redacts ``args``/``extra`` in-place before
        serialising.

        Returns the mutated entry so callers can cheaply inspect
        the final ``hmac`` (useful in tests and for linking to a
        downstream record).
        """
        with self._lock:
            key = self._load_key()
            entry.args = redact(entry.args)
            entry.extra = redact(entry.extra)
            entry.prev_hmac = self._last_hmac
            entry.hmac = _compute_hmac(key, entry)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._last_hmac = entry.hmac
            return entry

    def verify(self) -> VerifyResult:
        """Walk the log, re-compute each entry's HMAC, confirm
        the chain.

        Stops at the first bad line and reports its number. An
        empty or missing log is a valid pass (``ok=True, total=0``)."""
        if not self._path.exists():
            return VerifyResult(ok=True, total_lines=0)
        try:
            key = self._load_key()
        except FileNotFoundError:
            return VerifyResult(
                ok=False,
                total_lines=0,
                bad_line=0,
                reason="audit.key missing; cannot verify chain",
            )
        prev = ""
        total = 0
        with self._path.open("r", encoding="utf-8") as f:
            for i, raw in enumerate(f, start=1):
                total += 1
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    return VerifyResult(
                        ok=False,
                        total_lines=total,
                        bad_line=i,
                        reason="malformed JSON",
                    )
                # Reconstruct an AuditEntry shape (ignore unknown
                # fields so forward-compat adds don't break old
                # verify calls).
                expected_hmac = data.pop("hmac", "")
                recorded_prev = data.get("prev_hmac", "")
                if recorded_prev != prev:
                    return VerifyResult(
                        ok=False,
                        total_lines=total,
                        bad_line=i,
                        reason="prev_hmac mismatch (missing/inserted entry?)",
                    )
                try:
                    entry = AuditEntry(**{k: data.get(k) for k in _ENTRY_FIELDS if k in data})
                except TypeError as e:
                    return VerifyResult(
                        ok=False,
                        total_lines=total,
                        bad_line=i,
                        reason=f"entry shape invalid: {e}",
                    )
                computed = _compute_hmac(key, entry)
                if not hmac.compare_digest(computed, expected_hmac):
                    return VerifyResult(
                        ok=False,
                        total_lines=total,
                        bad_line=i,
                        reason="HMAC mismatch (content tampered?)",
                    )
                prev = expected_hmac
        return VerifyResult(ok=True, total_lines=total)

    def iter_entries(self) -> list[dict[str, Any]]:
        """Read the log as parsed dicts. Used by the CLI tail
        command. Malformed lines are skipped with a warning —
        verification is a separate call."""
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    _log.warning(
                        "audit.malformed_line",
                        extra={"line_sample": line[:120]},
                    )
        return out

    # ------------------------------------------------------------------ key

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        if self._key_path.exists():
            self._key = self._key_path.read_bytes()
            return self._key
        # Generate on first use. 0600 perms (POSIX) so only the
        # gateway user can read; on Windows the default user ACL
        # applies, which is equivalent for single-user installs.
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_bytes(_KEY_BYTES)
        self._key_path.write_bytes(generated)
        if os.name != "nt":
            try:
                self._key_path.chmod(0o600)
            except OSError as e:
                _log.warning(
                    "audit.key_chmod_failed",
                    extra={"path": str(self._key_path), "error": str(e)},
                )
        _log.info("audit.key_generated", extra={"path": str(self._key_path)})
        self._key = generated
        return self._key


# --------------------------------------------------------------- helpers


_ENTRY_FIELDS: tuple[str, ...] = (
    "ts",
    "session_key",
    "client",
    "tool",
    "args",
    "decision",
    "ok",
    "duration_ms",
    "error",
    "prev_hmac",
    "extra",
)
"""Field order for HMAC computation. Stable because the HMAC
must be reproducible; any new field gets appended here (never
inserted mid-list) so old entries still verify."""


def _canonical_bytes(entry: AuditEntry) -> bytes:
    """Deterministic serialisation of an entry minus its own
    HMAC. Used by both :func:`_compute_hmac` and :meth:`verify`.

    Using ``sort_keys=True`` + a fixed field order guarantees
    byte-for-byte reproducibility regardless of Python's dict
    iteration order. JSON with ensure_ascii=True keeps the
    bytes stable across platforms that might disagree on
    Unicode normalisation."""
    payload = {k: getattr(entry, k) for k in _ENTRY_FIELDS}
    return json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")


def _compute_hmac(key: bytes, entry: AuditEntry) -> str:
    return hmac.new(key, _canonical_bytes(entry), hashlib.sha256).hexdigest()


def _tail_last_hmac(path: Path) -> str:
    """Read the last non-empty line's ``hmac`` field. Used on
    startup to resume the chain across restarts. Returns ``""``
    if the file is missing, empty, or the last line is malformed
    (in which case the next ``append`` starts a fresh chain link
    from ``""``, but :meth:`verify` will flag the break)."""
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end == 0:
                return ""
            # Walk backwards to find the last line break.
            buf_size = min(4096, end)
            f.seek(end - buf_size)
            chunk = f.read(buf_size)
    except OSError:
        return ""
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        return ""
    if isinstance(last, dict):
        hmac_value = last.get("hmac")
        if isinstance(hmac_value, str):
            return hmac_value
    return ""


def default_audit_paths(fitt_home: Path) -> tuple[Path, Path]:
    """Conventional (log_path, key_path) pair under FITT_HOME."""
    return fitt_home / "audit.jsonl", fitt_home / "audit.key"


def new_entry(
    *,
    session_key: str,
    client: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    ok: bool,
    duration_ms: int = 0,
    error: str = "",
    extra: dict[str, Any] | None = None,
    ts: float | None = None,
) -> AuditEntry:
    """Convenience builder that timestamps and fills defaults.

    Most callers want ``ts`` = now and ``extra`` = {}; making them
    optional keeps the call sites short. Tests can override ``ts``
    for deterministic sequences."""
    return AuditEntry(
        ts=ts if ts is not None else time(),
        session_key=session_key,
        client=client,
        tool=tool,
        args=args,
        decision=decision,
        ok=ok,
        duration_ms=duration_ms,
        error=error,
        extra=dict(extra) if extra else {},
    )


def clone_entry(entry: AuditEntry, **fields: Any) -> AuditEntry:
    """Return a copy of ``entry`` with selected fields replaced.
    Useful for tests; ``dataclasses.replace`` suffices for
    production but a thin wrapper keeps imports local."""
    return replace(entry, **fields)
