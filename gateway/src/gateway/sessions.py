"""Session resolution.

Phase 2 v0: the only valid session id is ``main``. Any other value
in the ``X-FITT-Session`` header raises ``UnknownSession`` which
maps to HTTP 400.

Phase 2.5 will expand this module to support arbitrary session ids
created via ``fitt session new <name>``, with the set of valid ids
loaded from disk at startup. Phase 2's storage layer
(``MemoryStore``) already writes to ``sessions/<id>/history/...``
so the transition is purely additive.
"""

from __future__ import annotations

from fastapi import Request

from .errors import UnknownSession

DEFAULT_SESSION_ID = "main"

VALID_SESSION_IDS_V0: frozenset[str] = frozenset({DEFAULT_SESSION_ID})

SESSION_HEADER = "X-FITT-Session"


def resolve_session_id(request: Request) -> str:
    """Return the session id for this request.

    Header precedence:
      1. ``X-FITT-Session`` if set, validated against the v0 allow-list.
      2. Default to ``main``.
    """
    header_value = request.headers.get(SESSION_HEADER)
    if header_value is None or header_value == "":
        return DEFAULT_SESSION_ID
    session_id = header_value.strip()
    if session_id not in VALID_SESSION_IDS_V0:
        raise UnknownSession(session_id, sorted(VALID_SESSION_IDS_V0))
    return session_id
