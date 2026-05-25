"""Per-key secrets editing (F15).

The most security-sensitive surface in the dashboard. The
shape is deliberately different from F11-F14:

1. **Per-key forms, not whole-file.** Each editable key
   gets its own small form. Submitting one form replaces
   exactly one field. We never render an editable
   ``secrets.yaml`` textarea because rendering existing
   values into the response surface is the worst-case
   leak path.

2. **Never render existing values.** The "current value"
   cell shows ``"configured"`` or ``"not configured"`` —
   identical to the read-only redacted view on
   ``/dashboard/settings``. Each edit form's value input
   is empty; an empty submit means "no change" (skip).
   The dashboard's response body never contains the raw
   bytes of any secret.

3. **Double-confirm with bearer token.** Each form
   requires the operator to re-paste a bearer token
   alongside the new value. The bearer is compared via
   ``hmac.compare_digest`` against the configured
   ``allowed_tokens``. A long-lived dashboard cookie
   alone isn't enough authority for secrets edits.

4. **Dedicated audit category.** ``tool="dashboard.secret_set"``,
   ``args.key`` naming the path (e.g. ``"openrouter_api_key"``,
   ``"api_keys.qwen-big"``, ``"telegram.bot_token"``) but
   never the value. The chain captures *that* an edit
   happened, when, by whom — not what.

5. **Mode check.** The gateway refuses to load
   ``secrets.yaml`` if mode is anything but 0600 on POSIX.
   After the dashboard writes, we re-stat and re-chmod if
   needed so the gateway's next boot still loads cleanly.

6. **Restart-to-apply.** Same posture as F14. Saved
   secrets take effect on next boot. Yellow banner.

7. **No undo.** Once a new value is written, the old one
   is gone. The audit log captures the *edit*, not the
   old value. F15b could add an op-controlled rollback
   that requires a separate authorization step; not in v0.

What this commit does NOT do
----------------------------

* **No allowed_tokens CRUD.** Adding/removing/renaming
  bearer-token entries is a separate surface (token name
  uniqueness, client-tag uniqueness, can't remove the
  token you're currently using). Tracked as F15b.
* **No add-token-via-form.** F15b lands the multi-field
  form with the "you can't remove the token you're
  authed with" check.

Add-an-api-key is supported (separate form) because
``api_keys`` is a flat ``{model_id: key}`` mapping with no
relational constraints — the simplest case to extend.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from ..audit import AuditLog
from ..config import Secrets
from .actions import audit_action

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- the editable keys


_SCALAR_KEYS: tuple[str, ...] = (
    "openrouter_api_key",
    "anthropic_api_key",
)
"""Top-level scalar fields that take a single secret value."""


_TELEGRAM_KEY = "telegram.bot_token"
"""The one nested-but-still-a-scalar editable key on the
telegram block. ``allowlist_user_ids`` is a list of integer
ids — non-secret, edited via a separate form not in v0.
``telegram.bot_token`` IS a secret and gets the same
double-confirm / never-render-value treatment."""


# Allowlist of editable key paths. Anything else → 400.
EDITABLE_KEYS: frozenset[str] = frozenset({*_SCALAR_KEYS, _TELEGRAM_KEY})


def is_api_keys_path(key: str) -> bool:
    """``api_keys.<model_id>`` form. Returns True for any key
    of that shape — the model_id is operator-supplied, so we
    can't allowlist it by name."""
    return key.startswith("api_keys.") and "/" not in key and "\\" not in key


# --------------------------------------------------------------- bearer re-auth


def verify_bearer_reauth(secrets: Secrets, *, submitted: str) -> bool:
    """Compare ``submitted`` against every configured bearer
    via ``hmac.compare_digest``. Empty input → False
    (no skip-by-omission for the second factor).
    """
    if not submitted:
        return False
    submitted = submitted.strip()
    if not submitted:
        return False
    for entry in secrets.allowed_tokens:
        if hmac.compare_digest(entry.token, submitted):
            return True
    return False


# --------------------------------------------------------------- read state


def secret_presence(secrets: Secrets) -> dict[str, str]:
    """Return ``{key: "configured" | "not configured"}`` for
    every editable key. Used by the read view's table.

    Includes every existing ``api_keys.<id>`` entry plus the
    fixed top-level keys."""
    out: dict[str, str] = {}
    for k in _SCALAR_KEYS:
        v = getattr(secrets, k, None)
        out[k] = "configured" if v else "not configured"
    out[_TELEGRAM_KEY] = "configured" if (secrets.telegram is not None) else "not configured"
    for model_id in secrets.api_keys or {}:
        out[f"api_keys.{model_id}"] = "configured"
    return out


# --------------------------------------------------------------- write


class SecretsEditError(Exception):
    """Base for F15 edit failures. Carries an
    operator-readable detail and an HTTP status."""

    http_status: int = 500
    code: str = "secret_edit_error"


class BearerReauthFailed(SecretsEditError):
    http_status = 401
    code = "bearer_reauth_failed"


class UnknownKey(SecretsEditError):
    http_status = 400
    code = "unknown_key"


class InvalidValue(SecretsEditError):
    http_status = 400
    code = "invalid_value"


def write_secret_field(
    *,
    path: Path,
    key: str,
    new_value: str,
    audit_log: AuditLog | None,
    client: str,
) -> None:
    """Atomically replace exactly one field in ``secrets.yaml``.

    Reads the existing YAML (preserving every other field),
    sets ``key`` to ``new_value``, writes back via tmp+rename,
    re-chmods to 0600 on POSIX, audits with key but never value.

    ``key`` is one of:

    * ``"openrouter_api_key"``
    * ``"anthropic_api_key"``
    * ``"telegram.bot_token"``
    * ``"api_keys.<model_id>"``

    Anything else raises :class:`UnknownKey`.

    Empty ``new_value`` is treated as "remove this key" for
    api_keys; for the other scalars it's treated as "unset"
    (set to None / drop).

    No mtime check here — the ``secrets.yaml`` hot path is
    one operator on one machine; the audit log captures the
    sequence either way. If concurrent secrets edit becomes
    a real concern (say from two tabs), the F10 substrate's
    optimistic-mtime is the path forward.
    """
    if key not in EDITABLE_KEYS and not is_api_keys_path(key):
        raise UnknownKey(f"unknown editable key: {key!r}")

    started = time.time()

    # Read.
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except yaml.YAMLError as exc:
        _audit_secret(
            audit_log,
            key=key,
            client=client,
            ok=False,
            decision="error",
            error=f"YAML parse error: {exc}",
            duration_ms=int((time.time() - started) * 1000),
            extra={"reason": "yaml_parse_error"},
        )
        raise SecretsEditError(f"existing secrets.yaml is malformed: {exc}") from exc

    if not isinstance(raw, dict):
        raw = {}

    # Mutate.
    if key in _SCALAR_KEYS:
        if new_value:
            raw[key] = new_value
        else:
            raw.pop(key, None)
    elif key == _TELEGRAM_KEY:
        tg = raw.get("telegram")
        if not isinstance(tg, dict):
            tg = {}
        if new_value:
            tg["bot_token"] = new_value
            # Preserve allowlist_user_ids if present; the
            # operator's edit form only touches the bot
            # token.
            if "allowlist_user_ids" not in tg:
                tg["allowlist_user_ids"] = []
            raw["telegram"] = tg
        else:
            # Empty value → drop the entire telegram block
            # (matches the load_secrets shape; bot_token is
            # required by the TelegramSecrets model).
            raw.pop("telegram", None)
    elif is_api_keys_path(key):
        model_id = key.split(".", 1)[1]
        api_keys = raw.get("api_keys") or {}
        if not isinstance(api_keys, dict):
            api_keys = {}
        if new_value:
            api_keys[model_id] = new_value
        else:
            api_keys.pop(model_id, None)
        raw["api_keys"] = api_keys

    # Write atomically.
    payload = yaml.safe_dump(raw, default_flow_style=False, sort_keys=True, allow_unicode=True)
    tmp = path.with_suffix(path.suffix + ".secrets.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        # Chmod the tmp file BEFORE rename so the canonical
        # path's first existence is already 0600. Otherwise a
        # window opens where world-readable mode bits could
        # be inherited from the umask.
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)
            except OSError as exc:
                _log.warning(
                    "secrets.chmod_tmp_failed",
                    extra={"path": str(tmp), "error": str(exc)},
                )
        os.replace(tmp, path)
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                _log.warning(
                    "secrets.chmod_failed",
                    extra={"path": str(path), "error": str(exc)},
                )
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        _audit_secret(
            audit_log,
            key=key,
            client=client,
            ok=False,
            decision="error",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
            extra={"reason": "io_error"},
        )
        raise SecretsEditError(f"write failed: {type(exc).__name__}") from exc

    _audit_secret(
        audit_log,
        key=key,
        client=client,
        ok=True,
        decision="approved",
        error="",
        duration_ms=int((time.time() - started) * 1000),
        extra={"action": "set" if new_value else "unset"},
    )


def _audit_secret(
    audit_log: AuditLog | None,
    *,
    key: str,
    client: str,
    ok: bool,
    decision: str,
    error: str,
    duration_ms: int,
    extra: dict[str, Any],
) -> None:
    """Emit a ``dashboard.secret_set`` audit entry. ``args`` carries
    only the key path — never the value. The audit chain proves
    *that* a secret was edited; the value itself never reaches
    the chain (or the response surface, or the gateway's
    structured logs)."""
    audit_action(
        audit_log,
        tool="dashboard.secret_set",
        args={"key": key},
        client=client,
        ok=ok,
        decision=decision,
        error=error,
        duration_ms=duration_ms,
        extra=extra,
    )
