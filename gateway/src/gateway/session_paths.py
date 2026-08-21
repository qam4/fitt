"""One place that turns a session key into a directory name.

Every per-session store (history, turn events, turn captures, tool
artifacts) lays out its files as ``<sessions_dir>/<session>/<kind>/...``.
Four of them built that path by hand, and three got it wrong: a cron
firing's session key is ``cron:<id>:<ts>``, a colon is illegal in a
Windows path component, so on Windows every write raised ``OSError``,
was caught, logged at warning level, and dropped.

The visible cost was that ``fitt watch``, ``/lastturn``, the dashboard's
turn detail and turn capture were all blind to scheduled jobs — the
observability surface whose entire job is telling you what ran while you
weren't watching. Found 2026-08-19 while reading an eval log for the
cron least-privilege work: sixteen ``turns.append_failed`` per firing.
It had probably never worked on Windows, and no test noticed, because
the tests assert on the event log and the audit log rather than on turn
files.

**Why this replaces only filesystem-reserved characters**, rather than
reusing the stricter ``[A-Za-z0-9._-]`` allowlist in
:mod:`gateway.tool_artifacts`: a stricter rule would relocate session
directories that work today. A key containing ``@`` or ``+`` writes fine
on every platform, so rewriting it would orphan real history with no
defect to justify it. Restricting the substitution to characters that
*cannot* appear in a path means every currently-working directory keeps
its exact name and only the currently-failing ones change — from an
error into a file. Migration risk is nil by construction.

For a cron key the two rules happen to agree (``cron:a:1`` ->
``cron_a_1``), which is why ``tool_artifacts`` can adopt this helper for
its session component without moving anything either.
"""

from __future__ import annotations

import re
from pathlib import Path

_RESERVED_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
"""Characters that cannot appear in a path component on Windows.

``/`` and ``\\`` are included even though POSIX allows the former: a
session key carrying a separator would silently write into a
subdirectory, and one carrying ``..`` would climb out of
``sessions_dir`` altogether. Collapsing them to ``_`` makes a session
key exactly one directory level, everywhere.

NT device names (``CON``, ``NUL``, ``COM1``...) are deliberately not
handled. They would need a whole-name check rather than a character
substitution, and no session key FITT generates or accepts looks like
one. If that ever changes, this is the function to extend."""


def safe_session_dirname(session_key: str) -> str:
    """The directory name for ``session_key``.

    Empty or all-reserved input falls back to ``unknown`` so a caller
    never builds a path ending in the sessions root itself, which would
    scatter one session's files across every other session's parent."""
    cleaned = _RESERVED_PATH_CHARS.sub("_", session_key)
    # Windows silently strips trailing dots and spaces, so "foo." and
    # "foo" would collide and resolve to the same directory on one
    # platform and not the other.
    cleaned = cleaned.rstrip(". ")
    return cleaned or "unknown"


def session_dir(sessions_dir: Path, session_key: str) -> Path:
    """``<sessions_dir>/<safe session name>``.

    The single expression every per-session store should build on, so a
    future layout change happens in one place instead of four."""
    return sessions_dir / safe_session_dirname(session_key)
