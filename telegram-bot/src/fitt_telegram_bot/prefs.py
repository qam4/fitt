"""Per-chat preference store.

One JSON file mapping Telegram chat ids to a ``ChatPrefs`` struct.
Writes are atomic (tempfile + ``os.replace``) so a crash mid-save
cannot leave a half-written file.

Missing file -> empty set of prefs -> every chat uses defaults.
Corrupted JSON -> one warning log -> treated as empty. The file on
disk is not rewritten until the next successful ``save`` call.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_ALIAS = "fitt-default"
DEFAULT_SESSION = "main"


@dataclass(frozen=True)
class ChatPrefs:
    """Settings for one Telegram chat."""

    chat_id: int
    alias: str = DEFAULT_ALIAS
    session_id: str = DEFAULT_SESSION


class PrefsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[int, ChatPrefs] = {}
        self._load()

    # ---------- public API ----------------------------------------

    def get(self, chat_id: int) -> ChatPrefs:
        return self._cache.get(
            chat_id,
            ChatPrefs(chat_id=chat_id),
        )

    def set_alias(self, chat_id: int, alias: str) -> ChatPrefs:
        current = self.get(chat_id)
        updated = ChatPrefs(
            chat_id=chat_id,
            alias=alias,
            session_id=current.session_id,
        )
        self._cache[chat_id] = updated
        self._save()
        return updated

    def set_session(self, chat_id: int, session_id: str) -> ChatPrefs:
        current = self.get(chat_id)
        updated = ChatPrefs(
            chat_id=chat_id,
            alias=current.alias,
            session_id=session_id,
        )
        self._cache[chat_id] = updated
        self._save()
        return updated

    # ---------- internals -----------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log.warning(
                "telegram.prefs.read_failed",
                extra={"path": str(self._path), "error": str(e)},
            )
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("chats"), list):
            return
        for entry in raw["chats"]:
            if not isinstance(entry, dict):
                continue
            try:
                chat_id = int(entry["chat_id"])
            except (KeyError, TypeError, ValueError):
                continue
            self._cache[chat_id] = ChatPrefs(
                chat_id=chat_id,
                alias=str(entry.get("alias", DEFAULT_ALIAS)),
                session_id=str(entry.get("session_id", DEFAULT_SESSION)),
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "chats": [
                {
                    "chat_id": p.chat_id,
                    "alias": p.alias,
                    "session_id": p.session_id,
                }
                for p in sorted(self._cache.values(), key=lambda p: p.chat_id)
            ],
        }
        fd, tmp = tempfile.mkstemp(
            prefix="prefs-",
            suffix=".json.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
